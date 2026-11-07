"""Subscription periods, quota reservations, and verified provider events."""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Protocol
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .accounts import CurrentUser, Db, User
from .core import ApiError, as_utc, event, new_id, utcnow
from .db import Base

router = APIRouter(tags=["billing"])


class Checkout(Base):
    __tablename__ = "checkouts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    plan_code: Mapped[str] = mapped_column(String(32))
    checks_limit: Mapped[int]
    max_pages: Mapped[int]
    allow_followups: Mapped[bool]
    price_minor: Mapped[int | None]
    currency: Mapped[str | None] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_event_sequence: Mapped[int] = mapped_column(default=0)
    external_subscription_id: Mapped[str | None] = mapped_column(String(128))


class SubscriptionStatus(StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    EXPIRED = "expired"


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("status IN ('inactive','active','past_due')", name="ck_subscription_status"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    checkout_id: Mapped[str | None] = mapped_column(ForeignKey("checkouts.id", ondelete="SET NULL"), index=True)
    pending_checkout_id: Mapped[str | None] = mapped_column(
        ForeignKey("checkouts.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=SubscriptionStatus.INACTIVE)
    cancel_at_period_end: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UsagePeriod(Base):
    __tablename__ = "usage_periods"
    __table_args__ = (
        UniqueConstraint("checkout_id", "starts_at", name="uq_usage_period_checkout_start"),
        CheckConstraint("starts_at < ends_at", name="ck_usage_period_range"),
        CheckConstraint("checks_limit > 0 AND max_pages > 0", name="ck_usage_period_limits"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    checkout_id: Mapped[str] = mapped_column(ForeignKey("checkouts.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checks_limit: Mapped[int]
    max_pages: Mapped[int]
    allow_followups: Mapped[bool]


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_period_status", "period_id", "status"),
        CheckConstraint("status IN ('reserved','used','released')", name="ck_usage_record_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    period_id: Mapped[str | None] = mapped_column(ForeignKey("usage_periods.id", ondelete="CASCADE"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    free_pages: Mapped[int] = mapped_column(default=0)
    monthly_pages: Mapped[int] = mapped_column(default=0)
    extra_pages: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(8), default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SheetTopup(Base):
    __tablename__ = 'sheet_topups'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    checkout_id: Mapped[str] = mapped_column(ForeignKey('checkouts.id', ondelete='CASCADE'), unique=True)
    pages: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkout_id: Mapped[str] = mapped_column(ForeignKey("checkouts.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(24))
    sequence: Mapped[int]
    applied: Mapped[bool] = mapped_column(default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


PlanCode = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: PlanCode
    name: str = Field(min_length=1, max_length=100)
    checks_limit: int = Field(ge=1, le=1_000_000)
    max_pages: int = Field(ge=1, le=1000)
    allow_followups: bool
    price_minor: int | None = Field(default=None, ge=0, le=2_147_483_647)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def complete_price(self) -> "Plan":
        if (self.price_minor is None) != (self.currency is None):
            raise ValueError("price_minor and currency must be set together")
        return self


class PlanView(Plan):
    demonstration: bool


DEMO_PLAN = Plan(
    code="demo", name="Демонстрационный тариф", checks_limit=10, max_pages=10, allow_followups=True
)
SHEET_PLANS = {
    'monthly1000': Plan(code='monthly1000', name='Подписка на 1000 листов', checks_limit=1000,
                        max_pages=100, allow_followups=True, price_minor=1990, currency='BYN'),
    'topup140': Plan(code='topup140', name='Дополнительные 140 листов', checks_limit=140,
                     max_pages=100, allow_followups=True, price_minor=390, currency='BYN'),
}
plan_list = TypeAdapter(list[Plan])


def load_plans(config: str | None) -> dict[str, Plan]:
    plans = [DEMO_PLAN] if config is None else plan_list.validate_json(config)
    by_code = {plan.code: plan for plan in plans}
    if not by_code or len(plans) != len(by_code):
        raise ValueError("PLANS_JSON must contain at least one plan with a unique code")
    return by_code


@dataclass(frozen=True)
class Entitlement:
    max_pages: int
    allow_followups: bool


def lock_teacher(db: Session, user_id: str) -> None:
    # This write acquires a PostgreSQL row lock or a SQLite writer lock.
    db.execute(update(User).where(User.id == user_id).values(id=User.id))


def current_period(db: Session, subscription: Subscription | None) -> UsagePeriod | None:
    if subscription is None or subscription.checkout_id is None:
        return None
    now = utcnow()
    return db.scalar(
        select(UsagePeriod)
        .where(
            UsagePeriod.checkout_id == subscription.checkout_id,
            UsagePeriod.starts_at <= now,
            UsagePeriod.ends_at > now,
        )
        .order_by(UsagePeriod.starts_at.desc())
        .limit(1)
    )


def latest_period(db: Session, subscription: Subscription) -> UsagePeriod | None:
    if subscription.checkout_id is None:
        return None
    return db.scalar(
        select(UsagePeriod)
        .where(UsagePeriod.checkout_id == subscription.checkout_id)
        .order_by(UsagePeriod.starts_at.desc())
        .limit(1)
    )


def require_period(db: Session, user_id: str) -> UsagePeriod:
    subscription = db.get(Subscription, user_id)
    period = current_period(db, subscription)
    if period is None:
        raise ApiError(403, "subscription_required", "Для проверки нужна действующая подписка")
    return period


def entitlement(db: Session, user_id: str) -> Entitlement:
    period = require_period(db, user_id)
    return Entitlement(max_pages=period.max_pages, allow_followups=period.allow_followups)


def remaining_checks(db: Session, period: UsagePeriod) -> int:
    consumed = db.scalar(
        select(func.count()).select_from(UsageRecord).where(
            UsageRecord.period_id == period.id, UsageRecord.status.in_(("reserved", "used"))
        )
    )
    return max(0, period.checks_limit - consumed)


def sheet_balances(db: Session, user_id: str) -> tuple[int, int, int, UsagePeriod | None]:
    subscription = db.get(Subscription, user_id)
    checkout = db.get(Checkout, subscription.checkout_id) if subscription and subscription.checkout_id else None
    period = current_period(db, subscription) if checkout and checkout.plan_code == 'monthly1000' else None
    active = UsageRecord.status.in_(("reserved", "used"))
    free_used = db.scalar(select(func.coalesce(func.sum(UsageRecord.free_pages), 0)).where(UsageRecord.user_id == user_id, active))
    extra_used = db.scalar(select(func.coalesce(func.sum(UsageRecord.extra_pages), 0)).where(UsageRecord.user_id == user_id, active))
    extra_bought = db.scalar(select(func.coalesce(func.sum(SheetTopup.pages), 0)).where(SheetTopup.user_id == user_id))
    monthly_used = db.scalar(select(func.coalesce(func.sum(UsageRecord.monthly_pages), 0)).where(UsageRecord.period_id == period.id, active)) if period else 0
    return max(0, 30 - free_used), max(0, period.checks_limit - monthly_used) if period else 0, max(0, extra_bought - extra_used), period


def reserve_sheets(db: Session, user_id: str, pages: int) -> str:
    if pages < 1:
        raise ValueError('pages must be positive')
    lock_teacher(db, user_id)
    free, monthly, extra, period = sheet_balances(db, user_id)
    if pages > free + monthly + extra:
        raise ApiError(429, 'quota_exhausted', 'Недостаточно листов для проверки.')
    monthly_count = min(pages, monthly)
    free_count = min(pages - monthly_count, free)
    extra_count = pages - monthly_count - free_count
    record = UsageRecord(user_id=user_id, period_id=period.id if period and monthly_count else None,
                         monthly_pages=monthly_count, free_pages=free_count, extra_pages=extra_count)
    db.add(record)
    db.flush()
    return record.id


def reserve_check(db: Session, user_id: str) -> str:
    """Reserve one check inside the caller's transaction."""
    lock_teacher(db, user_id)
    period = require_period(db, user_id)
    if remaining_checks(db, period) == 0:
        raise ApiError(429, "quota_exhausted", "Лимит проверок на текущий период исчерпан")
    reservation = UsageRecord(period_id=period.id)
    db.add(reservation)
    db.flush()
    return reservation.id


def settle_check(db: Session, reservation_id: str, success: bool) -> None:
    db.execute(
        update(UsageRecord)
        .where(UsageRecord.id == reservation_id, UsageRecord.status == "reserved")
        .values(status="used" if success else "released", settled_at=utcnow())
    )


class PaymentType(StrEnum):
    SUCCEEDED = "payment_succeeded"
    RENEWED = "renewed"
    CANCELLED = "cancelled"
    FAILED = "payment_failed"


class PaymentNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9_:.\-]{1,128}$")
    checkout_id: str = Field(min_length=1, max_length=36)
    type: PaymentType
    sequence: int = Field(strict=True, ge=1, le=2_147_483_647)
    period_start: AwareDatetime | None = None
    period_end: AwareDatetime | None = None
    external_subscription_id: str | None = Field(default=None, min_length=1, max_length=128)
    amount_minor: int | None = Field(default=None, strict=True, ge=0, le=2_147_483_647)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def require_paid_period(self) -> "PaymentNotification":
        if self.type in (PaymentType.SUCCEEDED, PaymentType.RENEWED):
            if self.period_start is None or self.period_end is None or self.period_start >= self.period_end:
                raise ValueError("A paid event requires a valid period")
            if (self.amount_minor is None) != (self.currency is None):
                raise ValueError("A paid amount requires a currency")
        elif any(value is not None for value in (self.period_start, self.period_end, self.amount_minor, self.currency)):
            raise ValueError("Only paid events can establish a period or settled amount")
        return self


@dataclass(frozen=True)
class CheckoutIntent:
    checkout_id: str
    plan_code: str
    price_minor: int | None
    currency: str | None


class PaymentProvider(Protocol):
    name: str
    demonstration: bool

    def checkout(self, intent: CheckoutIntent) -> str:
        """Create or retrieve the same checkout using checkout_id as an idempotency key."""
        ...

    def cancel(self, external_subscription_id: str) -> None: ...

    def parse_event(self, raw_body: bytes, headers: Mapping[str, str]) -> PaymentNotification:
        """Authenticate the original bytes before normalizing a provider notification."""
        ...


class FakePaymentProvider:
    name = "fake"
    demonstration = True

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def checkout(self, intent: CheckoutIntent) -> str:
        self.require_secret()
        return f"/v1/payments/fake/checkout/{intent.checkout_id}"

    def cancel(self, external_subscription_id: str) -> None:
        # Fake has no external account; the application records the cancellation.
        pass

    def sign(self, body: bytes) -> str:
        self.require_secret()
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def require_secret(self) -> None:
        if not self._secret:
            raise ApiError(503, "payment_provider_unavailable", "Настройте тестовый платёжный провайдер")

    def parse_event(self, raw_body: bytes, headers: Mapping[str, str]) -> PaymentNotification:
        self.require_secret()
        signature = headers.get("x-payment-signature")
        if signature is None or not signature.isascii() or not hmac.compare_digest(self.sign(raw_body), signature):
            raise ApiError(401, "invalid_payment_signature", "Подпись платёжного события недействительна")
        try:
            return PaymentNotification.model_validate_json(raw_body)
        except ValidationError as exc:
            raise ApiError(422, "invalid_payment_event", "Некорректное платёжное событие") from exc


def payment_provider(request: Request) -> PaymentProvider:
    settings = request.app.state.settings
    provider = getattr(request.app.state, "payment_provider", None)
    if provider is None and settings.payment_provider == "fake":
        secret = settings.fake_payment_secret
        provider = FakePaymentProvider(secret.get_secret_value() if secret else "")
    if provider is None or (
        settings.deployment_mode != "local" and provider.name != settings.payment_provider
    ):
        raise ApiError(503, "payment_provider_unavailable", "Платёжный провайдер недоступен")
    if (provider.name == "fake" or provider.demonstration) and (
        settings.deployment_mode == "production"
        or settings.deployment_mode == "pilot" and not settings.allow_fake_payments
    ):
        raise ApiError(503, "payment_provider_unavailable", "Тестовые платежи отключены")
    return provider


Provider = Annotated[PaymentProvider, Depends(payment_provider)]


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_code: PlanCode


class CheckoutView(BaseModel):
    checkout_id: str
    checkout_url: str
    demonstration: bool


class FakeCheckoutView(BaseModel):
    checkout_id: str
    plan_code: str
    confirm_url: str
    demonstration: bool = True


class SubscriptionView(BaseModel):
    status: SubscriptionStatus
    plan_code: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    checks_limit: int = 0
    checks_remaining: int = 0
    max_pages: int | None = None
    allow_followups: bool = False
    cancel_at_period_end: bool = False
    demonstration: bool
    sheet_billing: bool = False
    free_remaining: int = 0
    monthly_remaining: int = 0
    extra_remaining: int = 0


class EventReceipt(BaseModel):
    duplicate: bool
    applied: bool
    demonstration: bool


def describe_subscription(db: Session, user_id: str, provider: PaymentProvider, *, sheets: bool = False, max_pages: int = 100) -> SubscriptionView:
    subscription = db.get(Subscription, user_id)
    extra_bought = db.scalar(select(func.coalesce(func.sum(SheetTopup.pages), 0)).where(SheetTopup.user_id == user_id)) if sheets else 0
    if subscription is None:
        if sheets:
            free, monthly, extra, _ = sheet_balances(db, user_id)
            return SubscriptionView(status=SubscriptionStatus.INACTIVE, demonstration=provider.demonstration,
                                    checks_remaining=free + monthly + extra, checks_limit=30 + extra_bought,
                                    max_pages=max_pages, allow_followups=True, sheet_billing=True,
                                    free_remaining=free, monthly_remaining=monthly, extra_remaining=extra)
        return SubscriptionView(status=SubscriptionStatus.INACTIVE, demonstration=provider.demonstration)
    current = current_period(db, subscription)
    period = current or latest_period(db, subscription)
    checkout = db.get(Checkout, subscription.checkout_id) if subscription.checkout_id else None
    if sheets and checkout and checkout.plan_code != 'monthly1000':
        current = period = None
    if current is not None:
        status = SubscriptionStatus(subscription.status)
    elif subscription.status == SubscriptionStatus.PAST_DUE:
        status = SubscriptionStatus.PAST_DUE
    elif period is not None and as_utc(period.ends_at) <= utcnow():
        status = SubscriptionStatus.EXPIRED
    else:
        status = SubscriptionStatus.INACTIVE
    free, monthly, extra, _ = sheet_balances(db, user_id) if sheets else (0, 0, 0, None)
    return SubscriptionView(
        status=status,
        plan_code=checkout.plan_code if checkout else None,
        period_start=as_utc(period.starts_at) if period else None,
        period_end=as_utc(period.ends_at) if period else None,
        checks_limit=(30 + (current.checks_limit if current else 0) + extra_bought) if sheets else (period.checks_limit if period else 0),
        checks_remaining=(free + monthly + extra) if sheets else (remaining_checks(db, current) if current else 0),
        max_pages=max_pages if sheets else (period.max_pages if period else None),
        allow_followups=True if sheets else (period.allow_followups if period else False),
        cancel_at_period_end=subscription.cancel_at_period_end,
        demonstration=checkout.provider == "fake" if checkout else provider.demonstration,
        sheet_billing=sheets, free_remaining=free, monthly_remaining=monthly, extra_remaining=extra,
    )


@router.get("/plans", response_model=list[PlanView])
def list_plans(request: Request, provider: Provider) -> list[PlanView]:
    return [
        PlanView(**plan.model_dump(), demonstration=provider.demonstration)
        for plan in payment_plans(request, provider).values()
    ]


def payment_plans(request: Request, provider: PaymentProvider) -> dict[str, Plan]:
    if request.app.state.settings.sheet_billing_enabled:
        return SHEET_PLANS
    config = request.app.state.settings.plans_json
    if not provider.demonstration and config is None:
        raise ApiError(503, "payment_catalog_unavailable", "Тарифы не настроены")
    return load_plans(config)


@router.get("/subscription", response_model=SubscriptionView)
def get_subscription(request: Request, db: Db, user_id: CurrentUser, provider: Provider) -> SubscriptionView:
    return describe_subscription(db, user_id, provider, sheets=request.app.state.settings.sheet_billing_enabled, max_pages=request.app.state.settings.max_pages)


@router.post("/subscription/checkout", status_code=201, response_model=CheckoutView)
def begin_checkout(
    body: CheckoutRequest, request: Request, db: Db, user_id: CurrentUser, provider: Provider
) -> CheckoutView:
    plan = payment_plans(request, provider).get(body.plan_code)
    if plan is None:
        raise ApiError(404, "plan_not_found", "Тариф не найден")
    if not provider.demonstration and plan.price_minor is None:
        raise ApiError(503, "payment_catalog_unavailable", "Для тарифа не настроена цена")
    lock_teacher(db, user_id)
    subscription = db.get(Subscription, user_id)
    if request.app.state.settings.sheet_billing_enabled and plan.code == 'topup140':
        _, monthly_remaining, _, current = sheet_balances(db, user_id)
        if subscription is None or subscription.status != SubscriptionStatus.ACTIVE or current is None or monthly_remaining < 1:
            raise ApiError(403, 'topup_unavailable', 'Докупка доступна при действующей подписке, пока в месячном пакете остаются листы.')
    if subscription is None:
        subscription = Subscription(user_id=user_id)
        db.add(subscription)
    period = latest_period(db, subscription)
    current_checkout = db.get(Checkout, subscription.checkout_id) if subscription.checkout_id else None
    legacy_upgrade = request.app.state.settings.sheet_billing_enabled and current_checkout and current_checkout.plan_code != 'monthly1000'
    if period and as_utc(period.ends_at) > utcnow() and not legacy_upgrade and not (request.app.state.settings.sheet_billing_enabled and plan.code == 'topup140'):
        raise ApiError(409, "subscription_already_paid", "Доступ по подписке уже оплачен")
    pending = db.get(Checkout, subscription.pending_checkout_id) if subscription.pending_checkout_id else None
    if pending and pending.plan_code == plan.code and pending.provider == provider.name:
        checkout = pending
    else:
        checkout = Checkout(user_id=user_id, provider=provider.name, plan_code=plan.code,
                            **plan.model_dump(exclude={"code", "name"}))
        db.add(checkout)
        db.flush()
        subscription.pending_checkout_id = checkout.id
    intent = CheckoutIntent(checkout.id, checkout.plan_code, checkout.price_minor, checkout.currency)
    # A provider may send a webhook before returning its hosted checkout URL.
    # Persist the correlation ID before calling it, including when a request needs retrying.
    db.commit()
    url = provider.checkout(intent)
    if not provider.demonstration:
        try:
            parsed = urlsplit(url)
            valid_url = (
                parsed.scheme == "https" and parsed.hostname is not None
                and parsed.username is None and parsed.password is None
                and not any(char.isspace() for char in url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ApiError(502, "invalid_checkout_url", "Платёжный провайдер вернул некорректный адрес")
    event("subscription_checkout_started", user_id=user_id, checkout_id=checkout.id, provider=provider.name)
    return CheckoutView(checkout_id=checkout.id, checkout_url=url, demonstration=provider.demonstration)


@router.post("/subscription/cancel", response_model=SubscriptionView)
def cancel_subscription(request: Request, db: Db, user_id: CurrentUser, provider: Provider) -> SubscriptionView:
    lock_teacher(db, user_id)
    subscription = db.get(Subscription, user_id)
    if subscription is None or subscription.checkout_id is None:
        raise ApiError(409, "no_subscription", "Нет подписки для отмены")
    checkout = db.get(Checkout, subscription.checkout_id)
    if checkout is None or checkout.provider != provider.name:
        raise ApiError(409, "payment_provider_unavailable", "Провайдер подписки недоступен")
    if not subscription.cancel_at_period_end:
        if not provider.demonstration and checkout.external_subscription_id is None:
            raise ApiError(409, "payment_subscription_missing", "Идентификатор подписки у провайдера отсутствует")
        provider.cancel(checkout.external_subscription_id or checkout.id)
        subscription.cancel_at_period_end = True
        db.commit()
        event("subscription_renewal_cancelled", user_id=user_id, checkout_id=checkout.id, provider=provider.name)
    return describe_subscription(db, user_id, provider, sheets=request.app.state.settings.sheet_billing_enabled, max_pages=request.app.state.settings.max_pages)


def find_checkout(db: Session, checkout_id: str, provider: PaymentProvider) -> Checkout:
    checkout = db.get(Checkout, checkout_id)
    if checkout is None or checkout.provider != provider.name:
        raise ApiError(404, "checkout_not_found", "Оформление подписки не найдено")
    return checkout


def apply_payment(
    db: Session, subscription: Subscription, checkout: Checkout, notification: PaymentNotification
) -> bool:
    if checkout.id not in (subscription.checkout_id, subscription.pending_checkout_id):
        return False
    if notification.sequence <= checkout.last_event_sequence:
        return False

    if checkout.plan_code == 'topup140' and notification.type == PaymentType.SUCCEEDED:
        if checkout.id != subscription.pending_checkout_id:
            return False
        db.add(SheetTopup(user_id=checkout.user_id, checkout_id=checkout.id, pages=checkout.checks_limit))
        subscription.pending_checkout_id = None
        return True

    if notification.type == PaymentType.CANCELLED:
        if checkout.id != subscription.checkout_id:
            subscription.pending_checkout_id = None
        else:
            subscription.cancel_at_period_end = True
        return True
    if notification.type == PaymentType.FAILED:
        if checkout.id == subscription.checkout_id or current_period(db, subscription) is None:
            subscription.status = SubscriptionStatus.PAST_DUE
        return True

    start, end = notification.period_start, notification.period_end
    if start is None or end is None:
        return False

    existing_period = latest_period(db, subscription)
    is_current_checkout = checkout.id == subscription.checkout_id
    if is_current_checkout and subscription.cancel_at_period_end:
        return False
    prior_checkout = db.get(Checkout, subscription.checkout_id) if subscription.checkout_id else None
    legacy_upgrade = checkout.plan_code == 'monthly1000' and prior_checkout and prior_checkout.plan_code != 'monthly1000'
    if existing_period and start < as_utc(existing_period.ends_at) and not legacy_upgrade:
        return False

    db.add(UsagePeriod(
        checkout_id=checkout.id,
        starts_at=as_utc(start),
        ends_at=as_utc(end),
        checks_limit=checkout.checks_limit,
        max_pages=checkout.max_pages,
        allow_followups=checkout.allow_followups,
    ))
    if not is_current_checkout:
        subscription.checkout_id = checkout.id
        subscription.pending_checkout_id = None
        subscription.cancel_at_period_end = False
    checkout.external_subscription_id = notification.external_subscription_id or checkout.external_subscription_id
    subscription.status = SubscriptionStatus.ACTIVE
    return True


def accept_event(db: Session, provider: PaymentProvider, notification: PaymentNotification) -> EventReceipt:
    checkout = find_checkout(db, notification.checkout_id, provider)
    user_id = checkout.user_id
    lock_teacher(db, user_id)
    # Another transaction may have changed the event version while we waited.
    db.refresh(checkout)
    subscription = db.get(Subscription, user_id)
    if subscription is None:
        raise ApiError(404, "checkout_not_found", "Оформление подписки не найдено")
    external_id = notification.external_subscription_id
    if (
        external_id and checkout.external_subscription_id and external_id != checkout.external_subscription_id
    ) or (
        not provider.demonstration
        and notification.type in (PaymentType.SUCCEEDED, PaymentType.RENEWED)
        and not external_id
    ):
        raise ApiError(409, "payment_subscription_mismatch", "Уведомление не соответствует подписке")
    if not provider.demonstration and notification.type in (PaymentType.SUCCEEDED, PaymentType.RENEWED):
        if checkout.price_minor is None or (notification.amount_minor, notification.currency) != (
            checkout.price_minor, checkout.currency,
        ):
            raise ApiError(409, "payment_amount_mismatch", "Сумма уведомления не соответствует тарифу")
    receipt = PaymentEvent(
        provider=provider.name, id=notification.id, checkout_id=checkout.id,
        event_type=notification.type, sequence=notification.sequence,
    )
    try:
        with db.begin_nested():
            db.add(receipt)
            db.flush()
    except IntegrityError as exc:
        previous = db.get(PaymentEvent, (provider.name, notification.id))
        if previous is None:
            raise
        if (previous.checkout_id, previous.event_type, previous.sequence) != (
            checkout.id, notification.type, notification.sequence,
        ):
            raise ApiError(409, "payment_event_conflict", "Идентификатор платёжного события уже используется") from exc
        db.commit()
        event("payment_event_received", user_id=user_id, checkout_id=checkout.id,
              event_type=previous.event_type, provider=provider.name, duplicate=True, applied=previous.applied)
        return EventReceipt(duplicate=True, applied=previous.applied, demonstration=provider.demonstration)

    receipt.applied = apply_payment(db, subscription, checkout, notification)
    if receipt.applied:
        checkout.last_event_sequence = notification.sequence
        subscription.updated_at = utcnow()
    db.commit()
    event("payment_event_received", user_id=user_id, checkout_id=checkout.id,
          event_type=notification.type, provider=provider.name, duplicate=False, applied=receipt.applied)
    return EventReceipt(duplicate=False, applied=receipt.applied, demonstration=provider.demonstration)


async def webhook_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16 * 1024:
            raise ApiError(413, "payment_event_too_large", "Платёжное событие слишком большое")
        body.extend(chunk)
    return bytes(body)


@router.post("/payments/webhook", response_model=EventReceipt, openapi_extra={
    "parameters": [{
        "name": "x-payment-signature", "in": "header", "required": False,
        "schema": {"type": "string"}, "description": "Подпись уведомления тестового провайдера",
    }],
})
def payment_webhook(
    request: Request,
    db: Db,
    provider: Provider,
    raw_body: Annotated[bytes, Depends(webhook_body)],
) -> EventReceipt:
    return accept_event(db, provider, provider.parse_event(raw_body, request.headers))


def owned_fake_checkout(
    db: Session, checkout_id: str, user_id: str, provider: PaymentProvider
) -> Checkout:
    if not isinstance(provider, FakePaymentProvider):
        raise ApiError(404, "checkout_not_found", "Оформление подписки не найдено")
    checkout = find_checkout(db, checkout_id, provider)
    if checkout.user_id != user_id:
        raise ApiError(404, "checkout_not_found", "Оформление подписки не найдено")
    return checkout


@router.get("/payments/fake/checkout/{checkout_id}", response_model=FakeCheckoutView)
def fake_checkout_view(checkout_id: str, db: Db, user_id: CurrentUser, provider: Provider) -> FakeCheckoutView:
    checkout = owned_fake_checkout(db, checkout_id, user_id, provider)
    return FakeCheckoutView(
        checkout_id=checkout.id, plan_code=checkout.plan_code,
        confirm_url=f"/v1/payments/fake/checkout/{checkout.id}/confirm",
    )


@router.post("/payments/fake/checkout/{checkout_id}/confirm", response_model=EventReceipt)
def fake_checkout_confirm(checkout_id: str, db: Db, user_id: CurrentUser, provider: Provider) -> EventReceipt:
    if not isinstance(provider, FakePaymentProvider):
        raise ApiError(404, "checkout_not_found", "Оформление подписки не найдено")
    checkout = owned_fake_checkout(db, checkout_id, user_id, provider)
    lock_teacher(db, user_id)
    db.refresh(checkout)
    event_id = f"fake-confirm-{checkout.id}"
    previous = db.get(PaymentEvent, (provider.name, event_id))
    if previous is not None:
        return EventReceipt(duplicate=True, applied=previous.applied, demonstration=True)
    subscription = db.get(Subscription, user_id)
    if subscription is None or subscription.pending_checkout_id != checkout.id:
        raise ApiError(409, "checkout_not_pending", "Оформление подписки уже завершено или заменено")
    if checkout.plan_code == 'topup140':
        _, monthly_remaining, _, current = sheet_balances(db, user_id)
        if subscription.status != SubscriptionStatus.ACTIVE or current is None or monthly_remaining < 1:
            raise ApiError(409, 'topup_unavailable', 'Докупка доступна при действующей подписке, пока в месячном пакете остаются листы.')
    now = utcnow()
    notification = PaymentNotification(
        id=event_id, checkout_id=checkout.id,
        type=PaymentType.SUCCEEDED, sequence=checkout.last_event_sequence + 1,
        period_start=now, period_end=now + timedelta(days=30),
    )
    body = notification.model_dump_json().encode()
    return accept_event(db, provider, provider.parse_event(body, {"x-payment-signature": provider.sign(body)}))


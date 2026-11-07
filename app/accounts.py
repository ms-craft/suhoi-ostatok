"""Local teacher accounts and expiring opaque bearer tokens."""

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .access_limits import enforce_auth_limit
from .core import ApiError, event, new_id, utcnow
from .db import Base, get_db

router = APIRouter(tags=["accounts"])
bearer = HTTPBearer(auto_error=False)
Db = Annotated[Session, Depends(get_db)]
password_hasher = PasswordHasher()
# A fixed dummy hash keeps missing-account verification on the same cost path.
dummy_hash = (
    "$argon2id$v=19$m=65536,t=3,p=4$FtluH998VLa3DtKRR5saIw$"
    "/RtHUkqMwiUsFfUIWRdFQlFbGWSAeuPAxxj8NtYTxyg"
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessToken(Base):
    __tablename__ = "access_tokens"

    digest: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256, repr=False)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        value = value.strip().casefold()
        if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", value):
            raise ValueError("Укажите корректный адрес электронной почты")
        return value


class Registration(Credentials):
    invite_code: SecretStr | None = Field(default=None, max_length=512, repr=False)


class AuthOptions(BaseModel):
    invite_required: bool


class AuthResult(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    expires_at: datetime


def issue_token(db: Session, user_id: str, ttl_seconds: int) -> AuthResult:
    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(seconds=ttl_seconds)
    db.add(AccessToken(digest=hashlib.sha256(token.encode()).digest(), user_id=user_id, expires_at=expires_at))
    db.commit()
    return AuthResult(access_token=token, user_id=user_id, expires_at=expires_at)


def current_user(
    db: Db,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    request: Request,
) -> str:
    if credentials is not None and len(credentials.credentials) == 43:
        digest = hashlib.sha256(credentials.credentials.encode()).digest()
        query = select(AccessToken.user_id).where(
            AccessToken.digest == digest, AccessToken.expires_at > utcnow(),
        )
        settings = request.app.state.settings
        if settings.deployment_mode == "pilot":
            query = query.join(User, AccessToken.user_id == User.id).where(
                User.email.in_(settings.pilot_allowed_emails),
            )
        user_id = db.scalar(query)
        if user_id is not None:
            return user_id
    raise ApiError(401, "authentication_required", "Войдите в аккаунт учителя")


CurrentUser = Annotated[str, Depends(current_user)]


@router.get("/auth/options", response_model=AuthOptions)
def auth_options(request: Request) -> AuthOptions:
    return AuthOptions(invite_required=request.app.state.settings.deployment_mode == "pilot")


@router.post("/auth/register", status_code=201, response_model=AuthResult)
def register(body: Registration, request: Request, db: Db) -> AuthResult:
    settings = request.app.state.settings
    if settings.deployment_mode == "pilot":
        code = body.invite_code.get_secret_value() if body.invite_code else ""
        expected = settings.pilot_access_code.get_secret_value()
        permitted = body.email in settings.pilot_allowed_emails
        if permitted:
            enforce_auth_limit(request, body.email, "enrollment")
        if not permitted or not secrets.compare_digest(code.encode(), expected.encode()):
            raise ApiError(403, "pilot_enrollment_required", "Для участия в пилоте нужно приглашение.")
    user = User(email=body.email, password_hash=password_hasher.hash(body.password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "account_exists", "Аккаунт с таким адресом уже существует") from exc
    result = issue_token(db, user.id, request.app.state.settings.auth_token_ttl_seconds)
    event("account_registered", user_id=user.id)
    return result


@router.post("/auth/login", response_model=AuthResult)
def login(body: Credentials, request: Request, db: Db) -> AuthResult:
    settings = request.app.state.settings
    if settings.deployment_mode == "pilot":
        if body.email not in settings.pilot_allowed_emails:
            raise ApiError(401, "invalid_credentials", "Неверный адрес или пароль")
        enforce_auth_limit(request, body.email, "login")
    user = db.scalar(select(User).where(User.email == body.email))
    expected_hash = user.password_hash if user else dummy_hash
    try:
        valid = password_hasher.verify(expected_hash, body.password)
    except (InvalidHashError, VerificationError):
        valid = False
    if not valid or user is None:
        raise ApiError(401, "invalid_credentials", "Неверный адрес или пароль")
    if password_hasher.check_needs_rehash(expected_hash):
        user.password_hash = password_hasher.hash(body.password)
    result = issue_token(db, user.id, request.app.state.settings.auth_token_ttl_seconds)
    event("account_authenticated", user_id=user.id)
    return result


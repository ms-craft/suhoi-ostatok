import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

import app.billing
from app.core import ApiError


def test_checkout_requires_verified_event_and_ignores_duplicate(harness):
    auth, _ = harness.register()
    plans = harness.client.get("/v1/plans")
    assert plans.status_code == 200
    plan, = plans.json()
    assert plan.items() >= {"code": "demo", "checks_limit": 1, "max_pages": 2, "demonstration": True}.items()
    assert harness.subscription(auth)["status"] == "inactive"
    assert harness.upload(auth).status_code == 403

    checkout = harness.checkout(auth)
    assert harness.client.get(checkout["checkout_url"], headers=auth).status_code == 200
    assert harness.subscription(auth)["status"] == "inactive"
    event = harness.event(checkout["checkout_id"])
    rejected = harness.webhook(event, secret="forged-signature")
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "invalid_payment_signature"
    assert harness.subscription(auth)["status"] == "inactive"

    verified = harness.webhook(event)
    assert verified.status_code == 200, verified.text
    assert verified.json().items() >= {"duplicate": False, "applied": True, "demonstration": True}.items()
    active = harness.subscription(auth)
    assert active.items() >= {"status": "active", "plan_code": "demo", "checks_remaining": 1}.items()
    assert active["period_start"] and active["period_end"]
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    repeated = harness.webhook(event)
    assert repeated.status_code == 200
    assert repeated.json()["duplicate"] is True
    assert harness.subscription(auth)["checks_remaining"] == 0
    exhausted = harness.upload(auth)
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["code"] == "quota_exhausted"


def test_subscription_and_fake_checkout_are_scoped_to_teacher(harness):
    owner, _ = harness.register()
    other, _ = harness.register()
    checkout = harness.checkout(owner)
    path = checkout["checkout_url"]
    assert harness.client.get(path, headers=other).status_code == 404
    assert harness.client.post(f"{path}/confirm", headers=other).status_code == 404
    assert harness.subscription(other)["status"] == "inactive"
    confirmed = harness.client.post(f"{path}/confirm", headers=owner)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["demonstration"] is True
    assert harness.subscription(owner)["status"] == "active"
    assert harness.subscription(other)["checks_remaining"] == 0
    assert harness.client.post("/v1/subscription/cancel", headers=other).status_code == 409
    assert harness.subscription(owner)["cancel_at_period_end"] is False


def test_cancel_retains_paid_access_through_period_and_stale_failure_is_ignored(harness, monkeypatch):
    auth, _ = harness.register()
    checkout = harness.checkout(auth)
    checkout_id = checkout["checkout_id"]
    failed = harness.webhook(harness.event(checkout_id, kind="payment_failed", sequence=1))
    assert failed.status_code == 200 and failed.json()["applied"]
    assert harness.subscription(auth)["status"] == "past_due"
    settled = harness.webhook(harness.event(checkout_id, sequence=3))
    assert settled.status_code == 200 and settled.json()["applied"]
    before_cancel = harness.subscription(auth)
    cancelled = harness.client.post("/v1/subscription/cancel", headers=auth)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json().items() >= {
        "cancel_at_period_end": True,
        "status": "active",
        "period_end": before_cancel["period_end"],
    }.items()
    stale = harness.webhook(harness.event(checkout_id, kind="payment_failed", sequence=2))
    assert stale.status_code == 200
    assert stale.json()["applied"] is False
    assert harness.subscription(auth)["status"] == "active"
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    assert harness.subscription(auth)["checks_remaining"] == 0
    expiry = datetime.fromisoformat(before_cancel["period_end"])
    monkeypatch.setattr(app.billing, "utcnow", lambda: expiry + timedelta(seconds=1))
    assert harness.subscription(auth)["status"] == "expired"
    denied = harness.upload(auth)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "subscription_required"


def test_new_period_has_fresh_quota_and_does_not_apply_early(harness, monkeypatch):
    auth, _ = harness.register()
    checkout_id = harness.checkout(auth)["checkout_id"]
    now = datetime.now(timezone.utc)
    boundary = now + timedelta(minutes=1)
    initial = harness.event(checkout_id, start=now - timedelta(minutes=1), end=boundary)
    assert harness.webhook(initial).json()["applied"] is True
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    assert harness.subscription(auth)["checks_remaining"] == 0

    renewal = harness.event(checkout_id, kind="renewed", sequence=2, start=boundary)
    assert harness.webhook(renewal).json()["applied"] is True
    assert harness.subscription(auth)["checks_remaining"] == 0
    monkeypatch.setattr(app.billing, "utcnow", lambda: boundary + timedelta(seconds=1))
    subscription = harness.subscription(auth)
    assert subscription["status"] == "active"
    assert subscription["checks_remaining"] == 1
    assert datetime.fromisoformat(subscription["period_start"]) == boundary
    assert harness.queued_submission(auth)["status"] == "queued"


def test_invalid_later_period_cannot_hide_valid_delayed_renewal(harness):
    auth, _ = harness.register()
    checkout_id = harness.checkout(auth)["checkout_id"]
    now = datetime.now(timezone.utc)
    boundary = now + timedelta(minutes=5)
    initial = harness.event(checkout_id, start=now - timedelta(minutes=1), end=boundary)
    assert harness.webhook(initial).json()["applied"] is True

    overlap = harness.event(checkout_id, kind="renewed", sequence=3, start=now)
    rejected = harness.webhook(overlap)
    assert rejected.status_code == 200
    assert rejected.json()["applied"] is False

    delayed = harness.event(checkout_id, kind="renewed", sequence=2, start=boundary)
    assert harness.webhook(delayed).json()["applied"] is True
    assert harness.webhook(overlap).json()["duplicate"] is True


def test_reused_event_id_with_another_sequence_conflicts(harness):
    auth, _ = harness.register()
    checkout_id = harness.checkout(auth)["checkout_id"]
    original = harness.event(checkout_id)
    assert harness.webhook(original).json()["applied"] is True
    conflicted = harness.webhook({**original, "sequence": 2})
    assert conflicted.status_code == 409
    assert conflicted.json()["error"]["code"] == "payment_event_conflict"


@pytest.mark.parametrize(
    ("mode", "allow_fake", "status"),
    [("local", False, 200), ("pilot", True, 200), ("pilot", False, 503), ("production", True, 503)],
)
def test_payment_route_defends_mode_even_with_injected_fake(harness, mode, allow_fake, status):
    # Deliberately bypass settings validation to exercise the request-time guard.
    harness.app.state.settings = harness.app.state.settings.model_copy(update={
        "deployment_mode": mode, "allow_fake_payments": allow_fake,
    })
    harness.app.state.payment_provider = app.billing.FakePaymentProvider(secrets.token_urlsafe(32))
    plans = harness.client.get("/v1/plans")
    assert plans.status_code == status
    if status == 200:
        assert all(plan["demonstration"] for plan in plans.json())
    else:
        assert plans.json()["error"]["code"] == "payment_provider_unavailable"


def test_hosted_adapter_uses_durable_checkout_terms_and_verified_external_id(harness):
    proof = secrets.token_urlsafe(24)
    intents = []
    cancelled = []

    class HostedAdapter:
        name = "hosted-test"
        demonstration = False

        def checkout(self, intent):
            intents.append(intent)
            if len(intents) == 1:
                raise ApiError(503, "provider_timeout", "Платёжный провайдер недоступен")
            if len(intents) == 2:
                return "http://checkout.example.invalid/session"
            return "https://checkout.example.invalid/session"

        def parse_event(self, raw_body, headers):
            if headers.get("x-provider-proof") != proof:
                raise ApiError(401, "invalid_payment_signature", "Подпись недействительна")
            return app.billing.PaymentNotification.model_validate_json(raw_body)

        def cancel(self, external_subscription_id):
            cancelled.append(external_subscription_id)

    harness.app.state.payment_provider = HostedAdapter()
    auth, _ = harness.register()
    path = "/v1/subscription/checkout"
    first = harness.client.post(path, headers=auth, json={"plan_code": "demo"})
    assert first.status_code == 503

    settings = harness.app.state.settings
    plans = json.loads(settings.plans_json)
    plans[0]["price_minor"] += 1
    harness.app.state.settings = settings.model_copy(update={"plans_json": json.dumps(plans)})
    invalid_url = harness.client.post(path, headers=auth, json={"plan_code": "demo"})
    assert invalid_url.status_code == 502
    assert invalid_url.json()["error"]["code"] == "invalid_checkout_url"
    again = harness.client.post(path, headers=auth, json={"plan_code": "demo"})
    assert again.status_code == 201, again.text
    assert again.json()["demonstration"] is False
    assert intents[0] == intents[1] == intents[2]
    assert intents[0].price_minor == plans[0]["price_minor"] - 1
    assert again.json()["checkout_id"] == intents[0].checkout_id
    assert harness.subscription(auth)["status"] == "inactive"

    notification = harness.event(intents[0].checkout_id)
    headers = {"X-Provider-Proof": proof}
    webhook = "/v1/payments/webhook"
    assert harness.client.post(webhook, json=notification).status_code == 401
    assert harness.client.post(webhook, json=notification, headers=headers).status_code == 409
    verified = {
        **notification, "external_subscription_id": "provider-subscription-for-test",
        "amount_minor": intents[0].price_minor, "currency": intents[0].currency,
    }
    wrong_amount = harness.client.post(webhook, json={
        **verified, "amount_minor": intents[0].price_minor + 1,
    }, headers=headers)
    assert wrong_amount.status_code == 409
    assert wrong_amount.json()["error"]["code"] == "payment_amount_mismatch"
    assert harness.subscription(auth)["status"] == "inactive"
    paid = harness.client.post(webhook, json=verified, headers=headers)
    assert paid.status_code == 200, paid.text
    assert paid.json() == {"duplicate": False, "applied": True, "demonstration": False}
    assert harness.client.post("/v1/subscription/cancel", headers=auth).status_code == 200
    assert cancelled == ["provider-subscription-for-test"]


@pytest.mark.parametrize("use_postgres", [False, True], ids=["sqlite", "postgres"])
def test_two_concurrent_uploads_cannot_reserve_last_check(build_harness, request, use_postgres):
    database_url = request.getfixturevalue("postgres_url") if use_postgres else None
    harness = build_harness(db_url=database_url)
    auth, _ = harness.register()
    harness.subscribe(auth)
    barrier = Barrier(2, timeout=10)

    def upload(_):
        barrier.wait()
        return harness.upload(auth)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(upload, range(2)))
    assert sorted(response.status_code for response in responses) == [202, 429], [
        response.text for response in responses
    ]
    rejected = next(response for response in responses if response.status_code == 429)
    assert rejected.json()["error"]["code"] == "quota_exhausted"
    accepted = next(response.json() for response in responses if response.status_code == 202)
    assert harness.subscription(auth)["checks_remaining"] == 0
    harness.process(accepted["job_id"])
    items = harness.client.get("/v1/submissions", headers=auth).json()["items"]
    assert [(item["id"], item["status"]) for item in items] == [(accepted["id"], "completed")]

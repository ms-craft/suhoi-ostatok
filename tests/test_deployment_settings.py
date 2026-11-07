from secrets import token_urlsafe

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.queue import MemoryJobQueue
from app.storage import MemoryStorage


@pytest.fixture
def pilot():
    return {
        "deployment_mode": "pilot",
        "public_origin": "https://pilot.example.invalid",
        "pilot_access_code": token_urlsafe(32),
        "pilot_allowed_emails": (" Teacher@Example.invalid ",),
        "allow_fake_payments": True,
        "fake_payment_secret": token_urlsafe(32),
        "ai_provider": "fake",
        "database_url": f"postgresql+psycopg://pilot:{token_urlsafe(24)}@postgres/pilot",
        "redis_url": f"redis://:{token_urlsafe(24)}@redis/0",
        "s3_endpoint_url": "https://s3.example.invalid",
        "s3_access_key": "pilot-service-id",
        "s3_secret_key": token_urlsafe(24),
    }


def test_pilot_requires_explicit_test_payment_and_normalizes_allowlist(pilot):
    settings = Settings(_env_file=None, **pilot)
    assert settings.deployment_mode == "pilot"
    assert settings.allow_fake_payments
    assert settings.pilot_allowed_emails == ("teacher@example.invalid",)


@pytest.mark.parametrize(
    "invalid, message",
    [
        ({"allow_fake_payments": False}, "explicit opt-in"),
        ({"fake_payment_secret": "demo-very-long-placeholder-for-local-only"}, "signing secret"),
        ({"fake_payment_secret": "stopkadr-local-payment-demo-secret"}, "signing secret"),
        ({"s3_secret_key": "stopkadr-local-storage-secret"}, "dedicated strong credential"),
        ({"pilot_allowed_emails": ()}, "allowlist"),
        ({"public_origin": "http://pilot.example.invalid"}, "HTTPS PUBLIC_ORIGIN"),
        ({"database_url": "postgresql://pilot:local-only@postgres/pilot"}, "dedicated strong credential"),
        ({"redis_url": "redis://redis/0"}, "authenticated Redis"),
        ({"s3_endpoint_url": "http://objects:9000"}, "HTTPS for the S3 endpoint"),
        ({"ai_provider": "gemini", "gemini_api_key": "test-key-placeholder"}, "ALLOW_EXTERNAL_AI"),
    ],
)
def test_pilot_rejects_insecure_configuration(pilot, invalid, message):
    with pytest.raises(ValidationError, match=message) as failure:
        Settings(_env_file=None, **(pilot | invalid))
    assert pilot["pilot_access_code"] not in str(failure.value)
    assert pilot["fake_payment_secret"] not in str(failure.value)
    assert pilot["database_url"] not in str(failure.value)


def test_pilot_requires_explicit_ai_provider(pilot):
    del pilot["ai_provider"]
    with pytest.raises(ValidationError, match="explicit AI_PROVIDER"):
        Settings(_env_file=None, **pilot)


def test_production_fails_closed_without_real_payments(pilot):
    with pytest.raises(ValidationError, match="real payment provider"):
        Settings(_env_file=None, **(pilot | {"deployment_mode": "production"}))


def test_local_keeps_demo_defaults():
    settings = Settings(_env_file=None)
    assert settings.deployment_mode == "local"
    assert settings.ai_provider == settings.payment_provider == "fake"


def test_pilot_does_not_expose_interactive_api_docs(pilot):
    settings = Settings(_env_file=None, **pilot)
    app = create_app(settings, storage=MemoryStorage(), queue=MemoryJobQueue())
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404

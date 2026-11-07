from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def deployment_secret(value: str | None, minimum: int) -> bool:
    if not value or len(value.strip()) < minimum:
        return False
    lowered = value.strip().casefold()
    return not lowered.startswith((
        "demo", "example", "local-only", "changeme", "replace", "test-only", "stopkadr-local",
    ))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True,
    )

    deployment_mode: Literal["local", "pilot", "production"] = "local"
    public_origin: str | None = None
    pilot_access_code: SecretStr | None = None
    pilot_allowed_emails: tuple[str, ...] = Field(default=(), repr=False)
    allow_fake_payments: bool = False
    allow_external_ai: bool = False

    database_url: str = Field(
        default="postgresql+psycopg://stopkadr:local-only@postgres:5432/stopkadr", repr=False,
    )
    redis_url: str = Field(default="redis://redis:6379/0", repr=False)
    queue_backend: Literal["redis", "memory"] = "redis"
    redis_queue_key: str = "stopkadr:processing:v1"
    storage_backend: Literal["s3", "memory"] = "s3"
    s3_endpoint_url: str | None = "http://objects:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "stopkadr-private"
    s3_access_key: str | None = Field(default=None, repr=False)
    s3_secret_key: SecretStr | None = None

    ai_provider: Literal["fake", "gemini"] = "fake"
    gemini_api_key: SecretStr | None = None
    gemini_api_base: str = "https://generativelanguage.googleapis.com/v1beta/models"
    gemini_model: str = "gemini-3.8-flash"
    gemini_recognition_model: str = "gemini-3.5-flash-lite"
    gemini_humanities_model: str = "gemini-3.8-flash"
    payment_provider: Literal["fake"] = "fake"
    fake_payment_secret: SecretStr | None = None
    plans_json: str | None = None
    auth_token_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    max_file_bytes: int = Field(default=12 * 1024 * 1024, ge=1, le=30 * 1024 * 1024)
    max_request_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    max_pages: int = Field(default=10, ge=1, le=20)
    max_image_pixels: int = Field(default=20_000_000, ge=1, le=40_000_000)
    page_image_edge: int = Field(default=2000, ge=500, le=3000)
    worker_poll_seconds: int = Field(default=3, ge=1, le=30)
    worker_heartbeat_path: Path | None = None
    job_lease_seconds: int = Field(default=180, ge=15, le=900)
    max_attempts: int = Field(default=3, ge=1, le=8)
    retry_delay_seconds: int = Field(default=10, ge=0, le=300)

    @field_validator("pilot_allowed_emails")
    @classmethod
    def normalize_pilot_emails(cls, emails: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(email.strip().casefold() for email in emails)
        if any("@" not in email or len(email) > 254 or any(char.isspace() for char in email)
               for email in normalized):
            raise ValueError("PILOT_ALLOWED_EMAILS must contain email addresses")
        return normalized

    @model_validator(mode="after")
    def check_provider_credentials(self):
        if self.ai_provider == "gemini" and not (
            self.gemini_api_key and self.gemini_api_key.get_secret_value().strip()
        ):
            raise ValueError("GEMINI_API_KEY is required for Gemini")
        if (self.s3_access_key is None) != (self.s3_secret_key is None):
            raise ValueError("S3_ACCESS_KEY and S3_SECRET_KEY must be provided together")
        if self.deployment_mode == "production":
            raise ValueError("Production requires a configured real payment provider")
        if self.deployment_mode == "pilot":
            self.check_pilot()
        return self

    def check_pilot(self) -> None:
        try:
            valid_origin = self.is_https_origin(self.public_origin)
            database = make_url(self.database_url)
            redis = urlsplit(self.redis_url)
        except (ArgumentError, ValueError, TypeError):
            raise ValueError("Invalid pilot service configuration") from None
        if not valid_origin:
            raise ValueError("Pilot requires an HTTPS PUBLIC_ORIGIN")
        if (not self.pilot_allowed_emails or not self.pilot_access_code or
                not deployment_secret(self.pilot_access_code.get_secret_value(), 32)):
            raise ValueError("Pilot requires a teacher allowlist and a strong PILOT_ACCESS_CODE")
        if (not self.allow_fake_payments or not self.fake_payment_secret or
                not deployment_secret(self.fake_payment_secret.get_secret_value(), 32)):
            raise ValueError("Pilot fake payments require explicit opt-in and a strong signing secret")
        if self.pilot_access_code == self.fake_payment_secret:
            raise ValueError("Pilot enrollment and payment signing require different secrets")
        if "ai_provider" not in self.model_fields_set:
            raise ValueError("Pilot requires an explicit AI_PROVIDER")
        if self.ai_provider == "gemini" and not self.allow_external_ai:
            raise ValueError("Pilot Gemini requires explicit ALLOW_EXTERNAL_AI")
        if database.get_backend_name() != "postgresql" or not deployment_secret(database.password, 24):
            raise ValueError("Pilot requires PostgreSQL with a dedicated strong credential")
        if (self.queue_backend != "redis" or redis.scheme not in {"redis", "rediss"} or
                not redis.hostname or not deployment_secret(unquote(redis.password or ""), 24)):
            raise ValueError("Pilot requires an authenticated Redis connection")
        if self.storage_backend != "s3" or not self.s3_secret_key or not deployment_secret(
            self.s3_secret_key.get_secret_value(), 24,
        ):
            raise ValueError("Pilot requires a private S3 bucket with a dedicated strong credential")
        if self.s3_endpoint_url and not self.is_https_origin(self.s3_endpoint_url):
            raise ValueError("Pilot requires HTTPS for the S3 endpoint")

    @staticmethod
    def is_https_origin(value: str | None) -> bool:
        try:
            origin = urlsplit(value or "")
            return bool(
                origin.scheme == "https" and origin.hostname and origin.username is None and
                origin.password is None and origin.path in {"", "/"} and not origin.query and
                not origin.fragment and origin.port != 0 and not any(char.isspace() for char in value or "")
            )
        except ValueError:
            return False


@lru_cache
def get_settings() -> Settings:
    return Settings()

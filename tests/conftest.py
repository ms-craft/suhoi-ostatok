import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db import Base
from app.main import create_app
from tests.support import Harness


@pytest.fixture
def database_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'stopkadr.db').as_posix()}"


@pytest.fixture
def postgres_url():
    configured_url = os.getenv("TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration")
    url = make_url(configured_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must select PostgreSQL")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    # Each test owns only this random schema, even on a shared test server.
    schema = f"stopkadr_test_{uuid4().hex}"
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    existing_options = url.query.get("options", "")
    isolated_url = url.update_query_dict({"options": f"{existing_options} -csearch_path={schema}".strip()})
    try:
        yield isolated_url.render_as_string(hide_password=False)
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()


@pytest.fixture
def build_harness(database_url):
    opened = []

    def build(*, ai_provider=None, db_url=None, allow_followups=True, **overrides):
        plan = {
            "code": "demo",
            "name": "Test plan",
            "checks_limit": 1,
            "max_pages": 2,
            "allow_followups": allow_followups,
            "price_minor": 100,
            "currency": "RUB",
        }
        fields = {
            "database_url": db_url or database_url,
            "fake_payment_secret": "test-signing-key-keep-out-of-logs-2026",
            "plans_json": json.dumps([plan]),
            "sheet_billing_enabled": False,
            "storage_backend": "memory",
            "queue_backend": "memory",
            "ai_provider": "fake",
            "payment_provider": "fake",
            "retry_delay_seconds": 0,
            "max_attempts": 3,
            "max_pages": 10,
            "max_file_bytes": 1024 * 1024,
            "max_request_bytes": 3 * 1024 * 1024,
        }
        settings = Settings(_env_file=None, **(fields | overrides))
        app = create_app(settings, ai_provider=ai_provider)
        engine = app.state.session_factory.kw["bind"]
        Base.metadata.create_all(engine)
        client = TestClient(app)
        opened.append((client, engine))
        return Harness(app, client, settings.fake_payment_secret.get_secret_value())

    yield build

    for client, engine in reversed(opened):
        client.close()
        engine.dispose()


@pytest.fixture
def harness(build_harness):
    return build_harness()

from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from secrets import token_urlsafe
from subprocess import TimeoutExpired
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import SecretStr
from redis.exceptions import ConnectionError

from app import files, orphans
from app.core import ApiError, utcnow
from app.db import make_session_factory
from app.object_lock import lock_object_writer, try_lock_object_cleanup
from app.storage import StoredObject
from tests.support import PASSWORD


class AuthCounter:
    def __init__(self):
        self.keys = {}

    def eval(self, script, key_count, key, timeout):
        assert "INCR" in script and "EXPIRE" in script
        assert key_count == 1 and timeout == 60
        self.keys[key] = self.keys.get(key, 0) + 1
        return self.keys[key]


@pytest.fixture
def enrollment(harness):
    email = "teacher@example.invalid"
    code = token_urlsafe(32)
    state = harness.app.state
    state.settings = state.settings.model_copy(update={
        "deployment_mode": "pilot",
        "pilot_access_code": SecretStr(code),
        "pilot_allowed_emails": (email,),
    })
    state.queue.client = AuthCounter()
    return {"email": email, "password": PASSWORD}, code


def test_invitation_is_required_for_allowed_address(harness, enrollment):
    credentials, code = enrollment
    assert harness.client.get("/v1/auth/options").json() == {"invite_required": True}
    for payload in (
        credentials,
        {**credentials, "invite_code": "incorrect"},
        {**credentials, "email": "unlisted@example.invalid", "invite_code": code},
    ):
        response = harness.client.post("/v1/auth/register", json={**payload, "full_name": "Иван Иванов"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "pilot_enrollment_required"
    registered = harness.client.post("/v1/auth/register", json={**credentials, "invite_code": code, "full_name": "Иван Иванов"})
    assert registered.status_code == 201, registered.text
    assert harness.client.post("/v1/auth/login", json=credentials).status_code == 200
    auth = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    assert harness.client.get("/v1/submissions", headers=auth).status_code == 200
    state = harness.app.state
    state.settings = state.settings.model_copy(update={"pilot_allowed_emails": ("other@example.invalid",)})
    assert harness.client.get("/v1/submissions", headers=auth).status_code == 401
    assert harness.client.post("/v1/auth/login", json=credentials).status_code == 401


def test_rate_limit_applies_across_requests_and_hides_address(harness, enrollment, monkeypatch):
    credentials, code = enrollment
    monkeypatch.setattr("app.access_limits.AUTH_ATTEMPTS", 2)
    for _ in range(2):
        assert harness.client.post("/v1/auth/login", json=credentials).status_code == 401
    response = harness.client.post("/v1/auth/login", json=credentials)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "auth_rate_limited"
    key, = harness.app.state.queue.client.keys
    assert credentials["email"] not in key and code not in key
    assert harness.client.post("/v1/auth/register", json={**credentials, "invite_code": code, "full_name": "Иван Иванов"}).status_code == 201


def test_pilot_auth_fails_closed_without_redis(harness, enrollment, monkeypatch):
    credentials, _ = enrollment

    def fail(*args):
        raise ConnectionError("synthetic private error")

    monkeypatch.setattr(harness.app.state.queue.client, "eval", fail)
    response = harness.client.post("/v1/auth/login", json=credentials)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_unavailable"
    assert "synthetic private error" not in response.text


def test_pilot_pdf_in_subprocess_preserves_pages_and_enforces_limit():
    first = Image.new("RGB", (70, 90), "red")
    second = Image.new("RGB", (70, 90), "blue")
    output = BytesIO()
    first.save(output, format="PDF", save_all=True, append_images=[second])
    pdf = output.getvalue()
    first_jpeg, second_jpeg = files.render_pilot_pdf(pdf, 2, 500)
    with Image.open(BytesIO(first_jpeg)) as image:
        assert image.convert("RGB").getpixel((0, 0))[0] > 240
    with Image.open(BytesIO(second_jpeg)) as image:
        assert image.convert("RGB").getpixel((0, 0))[2] > 240
    with pytest.raises(ApiError) as error:
        files.render_pilot_pdf(pdf, 1, 500)
    assert error.value.code == "page_limit"


def test_pilot_pdf_timeout_has_sanitized_child_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", token_urlsafe())
    monkeypatch.setenv("DATABASE_URL", token_urlsafe())

    def slow(command, **options):
        assert "GEMINI_API_KEY" not in options["env"]
        assert "DATABASE_URL" not in options["env"]
        assert options["timeout"] == files.PDF_TIMEOUT_SECONDS
        raise TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(files.subprocess, "run", slow)
    with pytest.raises(ApiError) as error:
        files.render_pilot_pdf(b"%PDF-synthetic", 1, 500)
    assert error.value.code == "pdf_render_timeout"


class FakeInventory:
    def __init__(self, items):
        self.items = items
        self.changed = set()
        self.deleted = []

    def iter_objects(self, prefix):
        return iter(item for item in self.items if item.key.startswith(prefix))

    def inspect_object(self, key):
        item = next(item for item in self.items if item.key == key)
        return replace(
            item,
            etag="new" if key in self.changed else item.etag,
            modified_at=item.modified_at.replace(microsecond=0),
        )

    def delete_unchanged(self, item):
        self.deleted.append(item.key)
        return True


def object_item(*, key=None, hours=72):
    key = key or f"submissions/{uuid4()}/{uuid4()}/original/1"
    return StoredObject(key, utcnow() - timedelta(hours=hours), '"sample-etag"', 100)


def test_orphan_scan_skips_live_recent_and_unknown_keys(harness):
    auth, _ = harness.register()
    harness.subscribe(auth)
    harness.queued_submission(auth)
    state = harness.app.state
    live = [object_item(key=key) for key in state.storage.objects]
    recent, orphan = object_item(hours=1), object_item()
    inventory = FakeInventory([*live, recent, orphan, object_item(key="submissions/unknown")])
    with state.session_factory.begin() as db:
        counts = orphans.reconcile(db, inventory)
        assert (counts.scanned, counts.candidates, counts.removed) == (5, 1, 0)
        assert inventory.deleted == []
        with pytest.raises(orphans.CleanupUnavailable, match="PostgreSQL"):
            orphans.reconcile(db, inventory, apply=True)


def test_orphan_apply_revalidates_object_and_honors_bound(harness, monkeypatch):
    changed, valid, more = object_item(), object_item(), object_item()
    inventory = FakeInventory([changed, valid, more])
    inventory.changed.add(changed.key)
    monkeypatch.setattr(orphans, "try_lock_object_cleanup", lambda db: True)
    with harness.app.state.session_factory.begin() as db:
        counts = orphans.reconcile(db, inventory, apply=True, max_delete=1)
        assert counts.changed == counts.removed == 1
        assert counts.delete_limited
        assert inventory.deleted == [valid.key]


def test_postgres_orphan_apply_cannot_overlap_active_upload(postgres_url):
    factory = make_session_factory(postgres_url)
    with factory.begin() as upload, factory.begin() as cleanup:
        lock_object_writer(upload)
        assert not try_lock_object_cleanup(cleanup)
    with factory.begin() as cleanup:
        assert try_lock_object_cleanup(cleanup)

import asyncio
import json
import logging

import httpx
import pytest

from app.ai import AIError, FakeAIProvider, RecognizedTask
from app.core import JsonFormatter
from tests.support import PASSWORD, image_bytes, photograph


def test_work_pages_originals_results_and_mutations_require_ownership(harness):
    owner, _ = harness.register()
    outsider, _ = harness.register()
    harness.subscribe(owner)
    submission = harness.queued_submission(owner)
    harness.process(submission["job_id"])
    task, = harness.results(owner, submission["id"])["tasks"]
    base = f"/v1/submissions/{submission['id']}"
    task_path = f"{base}/tasks/{task['id']}"
    for path in (base, f"{base}/pages/1", f"{base}/files/1", f"{base}/results",
                 f"{base}/jobs/{submission['job_id']}"):
        assert harness.client.get(path).status_code == 401
        response = harness.client.get(path, headers=outsider)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    for method, path, payload in (
        ("DELETE", base, {}),
        ("PATCH", task_path, {"json": {"answer": "other"}}),
        ("POST", f"{task_path}/confirm", {}),
        ("POST", f"{task_path}/recheck", {}),
        ("POST", f"{base}/clarifications", {"data": {"text": "other"}}),
    ):
        response = harness.client.request(method, path, headers=outsider, **payload)
        assert response.status_code == 404, response.text
    assert harness.client.get("/v1/submissions", headers=outsider).json()["items"] == []
    assert harness.results(owner, submission["id"])["tasks"][0]["confirmed"] is False


@pytest.mark.parametrize("token", [None, "invalid-opaque-token"])
def test_subscription_rejects_absent_or_invalid_auth(harness, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = harness.client.get("/v1/subscription", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("filename", "body", "mime", "status", "error_code"),
    [
        ("fake.jpg", b"<html>text</html>", "image/jpeg", 415, "unsupported_file"),
        ("lie.jpg", image_bytes(), "image/jpeg", 415, "unsupported_file"),
        ("broken.png", b"\x89PNG\r\n\x1a\ninvalid", "image/png", 422, "invalid_file"),
        ("large.jpg", b"x" * 2049, "image/jpeg", 413, "file_too_large"),
    ],
)
def test_rejected_uploads_do_not_consume_quota_or_store_content(
    build_harness, filename, body, mime, status, error_code,
):
    harness = build_harness(max_file_bytes=2048)
    auth, _ = harness.register()
    harness.subscribe(auth)
    response = harness.upload(auth, ("files", (filename, body, mime)))
    assert response.status_code == status
    assert response.json()["error"]["code"] == error_code
    assert not harness.app.state.storage.objects
    assert harness.subscription(auth)["checks_remaining"] == 1


def test_chunked_multipart_body_cannot_bypass_request_limit(build_harness):
    harness = build_harness(max_request_bytes=1024)
    auth, _ = harness.register()
    harness.subscribe(auth)

    async def multipart():
        yield b'--limit\r\nContent-Disposition: form-data; name="files"; filename="photo.jpg"\r\n'
        yield b"Content-Type: image/jpeg\r\n\r\n" + b"x" * 650
        yield b"x" * 650 + b"\r\n--limit--\r\n"

    async def upload():
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/v1/submissions", content=multipart(),
                headers={**auth, "Content-Type": "multipart/form-data; boundary=limit"},
            )

    response = asyncio.run(upload())
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "request_too_large"
    assert harness.subscription(auth)["checks_remaining"] == 1
    assert not harness.app.state.storage.objects


def test_plan_enforces_page_and_followup_limits(build_harness):
    harness = build_harness(allow_followups=False)
    auth, _ = harness.register()
    harness.subscribe(auth)
    excess = harness.upload(auth, photograph(), photograph(), photograph())
    assert excess.status_code == 413
    assert excess.json()["error"]["code"] == "page_limit"
    assert harness.subscription(auth)["checks_remaining"] == 1
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    response = harness.client.post(
        f"/v1/submissions/{submission['id']}/clarifications", headers=auth, data={"text": "more"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "followups_unavailable"


def test_logs_correlate_job_and_hide_sensitive_payloads(build_harness, caplog):
    private_text = "worksheet-synthetic-private-answer"
    private_filename = "synthetic-student-name.jpg"
    synthetic_card = "4242424242424242"

    class BadProvider(FakeAIProvider):
        def recognize(self, pages, clarification):
            if clarification:
                raise AIError(f"private model payload: {clarification}")
            return [RecognizedTask(page_number=1, answer=private_text, answer_state="present")]

    harness = build_harness(ai_provider=BadProvider())
    with caplog.at_level(logging.INFO, logger="stopkadr"):
        auth, credentials = harness.register()
        event = harness.subscribe(auth)
        rejected = harness.webhook({**event, "card_number": synthetic_card})
        assert rejected.status_code == 422
        upload = harness.upload(auth, photograph(filename=private_filename))
        assert upload.status_code == 202
        submission = upload.json()
        harness.process(submission["job_id"])
        assert harness.results(auth, submission["id"])["tasks"][0]["recognition"]["answer"] == private_text
        clarification = harness.client.post(
            f"/v1/submissions/{submission['id']}/clarifications",
            headers=auth, data={"text": private_text},
        )
        assert clarification.status_code == 202
        harness.process(clarification.json()["job_id"])
        assert harness.submission(auth, submission["id"])["error_code"] == "ai_error"

    encoded = [JsonFormatter().format(record) for record in caplog.records if record.name == "stopkadr"]
    for secret in (private_text, private_filename, synthetic_card, harness.payment_secret,
                   auth["Authorization"], PASSWORD, credentials["email"]):
        assert all(secret not in line for line in encoded)
    events = [json.loads(line) for line in encoded]
    assert any(item["event"] == "payment_event_received" for item in events)
    assert any(item["event"] == "job_failed" and item["error_code"] == "ai_error" for item in events)
    completed = next(item for item in events if item["event"] == "job_completed")
    assert completed["component"] == "worker"
    assert completed["job_id"] == submission["job_id"]
    assert completed["submission_id"] == submission["id"]
    assert completed["request_id"] == upload.headers["X-Request-ID"]

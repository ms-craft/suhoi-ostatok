from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from threading import Event

import pytest
from PIL import Image

from app.ai import AIError, FakeAIProvider, RecognizedTask
from app.core import new_id, utcnow
from app.submission_models import ProcessingJob, Submission
from app.work import due_jobs
from tests.support import document_pixel, image_bytes, photograph


def test_photo_lifecycle_keeps_initial_work_and_deletes_blobs(harness):
    auth, credentials = harness.register()
    login = harness.client.post("/v1/auth/login", json=credentials)
    assert login.status_code == 200
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    harness.subscribe(auth)

    photo = photograph("red")
    submission = harness.queued_submission(auth, photo)
    submission_id = submission["id"]
    assert submission["page_count"] == 1
    assert submission["student_reference"] is None
    labelled = harness.client.patch(
        f"/v1/submissions/{submission_id}", headers=auth, json={"student_reference": "class-7a-01"},
    )
    assert labelled.status_code == 200
    assert labelled.json()["student_reference"] == "class-7a-01"
    assert harness.subscription(auth)["checks_remaining"] == 0
    harness.process(submission["job_id"])
    assert harness.submission(auth, submission_id)["status"] == "completed"

    result = harness.results(auth, submission_id)
    assert result["demonstration"] is True
    task, = result["tasks"]
    assert task["recognition"]["page_number"] == 1
    assert task["recognition"]["condition"] == "2 + 2"
    assert task["recognition"]["answer"] == "5"
    assert task["preliminary"]["status"] == "incorrect"
    assert task["preliminary"]["expected_answer"] == "4"
    assert task["preliminary"]["explanation"]
    assert task["teacher"] is None
    assert task["confirmed"] is False

    base = f"/v1/submissions/{submission_id}"
    path = f"{base}/tasks/{task['id']}"
    page = harness.client.get(f"{base}/pages/1", headers=auth)
    assert page.status_code == 200
    with Image.open(BytesIO(page.content)) as image:
        red, green, blue = image.convert("RGB").getpixel((0, 0))
        assert red > 240 and green < 10 and blue < 10

    correction = {"answer": "4", "status": "correct", "explanation": "Исправлено учителем"}
    edited = harness.client.patch(path, headers=auth, json=correction)
    assert edited.status_code == 200, edited.text
    task, = harness.results(auth, submission_id)["tasks"]
    assert task["recognition"]["answer"] == "5"
    assert task["preliminary"]["status"] == "incorrect"
    assert task["teacher"].items() >= correction.items()
    assert task["confirmed"] is False

    confirmation = harness.client.post(f"{path}/confirm", headers=auth)
    assert confirmation.status_code == 200, confirmation.text
    task, = harness.results(auth, submission_id)["tasks"]
    assert task["confirmed"] is True
    assert task["teacher"]["confirmed_at"] is not None
    assert task["teacher"]["final_status"] == "correct"
    listed = harness.client.get("/v1/submissions", headers=auth).json()["items"]
    assert [item["id"] for item in listed] == [submission_id]
    assert listed[0]["student_reference"] == "class-7a-01"

    assert harness.app.state.storage.objects
    deleted = harness.client.delete(base, headers=auth)
    assert deleted.status_code == 204, deleted.text
    assert harness.client.get(base, headers=auth).status_code == 404
    assert harness.client.get(f"{base}/pages/1", headers=auth).status_code == 404
    assert harness.client.get("/v1/submissions", headers=auth).json()["items"] == []
    assert not harness.app.state.storage.objects
    assert harness.subscription(auth)["checks_remaining"] == 0


def test_recheck_keeps_demonstration_label_for_original_recognition(harness):
    class OtherProvider(FakeAIProvider):
        name = "other-test-provider"
        demonstration = False

    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    submission_id = submission["id"]
    original, = harness.results(auth, submission_id)["tasks"]

    harness.app.state.ai_provider = OtherProvider()
    response = harness.client.post(
        f"/v1/submissions/{submission_id}/tasks/{original['id']}/recheck", headers=auth,
    )
    assert response.status_code == 202
    harness.process(response.json()["job_id"])
    result = harness.results(auth, submission_id)
    task, = result["tasks"]
    assert task["provider"] == "other-test-provider"
    assert task["original_recognition"] == original["recognition"]
    assert task["demonstration"] is True
    assert result["demonstration"] is True


def test_upload_storage_failure_rolls_back_files_and_reservation(harness, monkeypatch):
    auth, _ = harness.register()
    harness.subscribe(auth)
    storage = harness.app.state.storage
    original_put = storage.put

    def fail_page(key, content, mime_type):
        if "/pages/" in key:
            raise OSError("simulated upload failure")
        original_put(key, content, mime_type)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "put", fail_page)
        response = harness.upload(auth)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upload_unavailable"
    assert not storage.objects
    assert harness.client.get("/v1/submissions", headers=auth).json()["items"] == []
    assert harness.subscription(auth)["checks_remaining"] == 1
    assert harness.queued_submission(auth)["status"] == "queued"


@pytest.mark.parametrize("as_pdf", [False, True], ids=["photos", "pdf"])
def test_multipage_upload_keeps_page_order_and_task_sources(harness, as_pdf):
    auth, _ = harness.register()
    harness.subscribe(auth)
    if as_pdf:
        with BytesIO() as output:
            first = Image.new("RGB", (60, 70), "red")
            second = Image.new("RGB", (80, 70), "blue")
            first.save(output, format="PDF", save_all=True, append_images=[second])
            files = [("files", ("worksheet.pdf", output.getvalue(), "application/pdf"))]
    else:
        files = [
            ("files", ("second-by-name.png", image_bytes("red"), "image/png")),
            ("files", ("first-by-name.png", image_bytes("blue"), "image/png")),
        ]

    submission = harness.queued_submission(auth, *files)
    assert submission["page_count"] == 2
    harness.process(submission["job_id"])
    task_pages = [
        task["recognition"]["page_number"]
        for task in harness.results(auth, submission["id"])["tasks"]
    ]
    assert task_pages == [1, 2]
    base = f"/v1/submissions/{submission['id']}/pages"
    pages = [harness.client.get(f"{base}/{index}", headers=auth) for index in (1, 2)]
    assert [page.status_code for page in pages] == [200, 200]
    if as_pdf:
        sizes = []
        for page in pages:
            with Image.open(BytesIO(page.content)) as image:
                sizes.append(image.size)
        assert sizes[0][0] < sizes[1][0]
        assert sizes[0][1] > sizes[1][1]
    else:
        for page, channel in zip(pages, (0, 2), strict=True):
            color = document_pixel(page.content)
            assert color[channel] > 240 and sum(color) - color[channel] < 10


@pytest.mark.parametrize(
    ("recognition", "status"),
    [
        ({"answer": "5", "answer_state": "present"}, "needs_review"),
        ({"condition": "2 + 2", "answer_state": "unreadable"}, "unreadable"),
        ({"condition": "2 + 2", "answer_state": "unknown"}, "needs_review"),
        ({"condition": "2 + 2", "answer_state": "blank"}, "no_answer"),
        ({"condition": "What changed?", "answer": "The situation", "answer_state": "present",
          "ambiguity": "The text refers to two events", "kind": "open"}, "needs_review"),
        ({"condition": "Write a short essay", "answer": "An essay draft", "answer_state": "present",
          "kind": "open"}, "needs_review"),
    ],
    ids=["missing-condition", "unreadable", "unclear-empty", "known-empty", "ambiguous", "open-answer"],
)
def test_unresolved_recognition_is_reviewable(build_harness, recognition, status):
    provider = FakeAIProvider(fixtures=[RecognizedTask(page_number=1, **recognition)])
    harness = build_harness(ai_provider=provider)
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    task, = harness.results(auth, submission["id"])["tasks"]
    preliminary = task["preliminary"]
    assert preliminary["status"] == status
    assert preliminary["expected_answer"] is None
    assert preliminary["explanation"]
    if status in {"needs_review", "unreadable"}:
        assert preliminary["uncertainty"] or preliminary["missing_data"]
    assert task["confirmed"] is False
    assert task["recognition"]["region"] is None


def test_teacher_recheck_changes_suggestion_without_charging_again(harness):
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    task, = harness.results(auth, submission["id"])["tasks"]
    path = f"/v1/submissions/{submission['id']}/tasks/{task['id']}"
    correction = harness.client.patch(path, headers=auth, json={"condition": "2 + 3", "answer": "5"})
    assert correction.status_code == 200, correction.text
    recheck = harness.client.post(f"{path}/recheck", headers=auth)
    assert recheck.status_code == 202, recheck.text
    harness.process(recheck.json()["job_id"])

    updated, = harness.results(auth, submission["id"])["tasks"]
    assert updated["id"] == task["id"]
    assert updated["recognition"]["condition"] == "2 + 2"
    assert updated["teacher"]["condition"] == "2 + 3"
    assert updated["preliminary"]["status"] == "correct"
    assert updated["preliminary"]["expected_answer"] == "5"
    assert updated["confirmed"] is False
    assert harness.subscription(auth)["checks_remaining"] == 0


def test_blank_teacher_edits_yield_needs_review_instead_of_failed_job(harness):
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    task, = harness.results(auth, submission["id"])["tasks"]
    path = f"/v1/submissions/{submission['id']}/tasks/{task['id']}"
    edited = harness.client.patch(path, headers=auth, json={"condition": "", "answer": "  "})
    assert edited.status_code == 200
    recheck = harness.client.post(f"{path}/recheck", headers=auth)
    assert recheck.status_code == 202
    harness.process(recheck.json()["job_id"])
    assert harness.submission(auth, submission["id"])["status"] == "completed"
    updated, = harness.results(auth, submission["id"])["tasks"]
    assert updated["original_recognition"]["answer"] == "5"
    assert updated["preliminary"]["status"] == "needs_review"
    assert harness.subscription(auth)["checks_remaining"] == 0


class ClarificationProvider(FakeAIProvider):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def recognize(self, pages, clarification):
        self.inputs.append((tuple(page.number for page in pages), clarification))
        return [
            RecognizedTask(
                page_number=1,
                number="1",
                condition="2 + 2" if len(pages) == 2 and clarification else None,
                answer="4",
                answer_state="present",
                kind="arithmetic",
            )
        ]


def test_adding_missing_page_and_text_reruns_without_charging(build_harness):
    provider = ClarificationProvider()
    harness = build_harness(ai_provider=provider)
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    initial, = harness.results(auth, submission["id"])["tasks"]
    assert initial["preliminary"]["status"] == "needs_review"

    path = f"/v1/submissions/{submission['id']}/clarifications"
    text = "Условие первого задания на второй странице."
    followup = harness.client.post(path, headers=auth, data={"text": text}, files=[photograph("blue")])
    assert followup.status_code == 202, followup.text
    harness.process(followup.json()["job_id"])
    assert provider.inputs == [((1,), None), ((1, 2), text)]
    assert harness.submission(auth, submission["id"])["page_count"] == 2
    updated, = harness.results(auth, submission["id"])["tasks"]
    assert updated["id"] == initial["id"]
    assert updated["original_recognition"]["condition"] is None
    assert updated["original_preliminary"]["status"] == "needs_review"
    assert updated["recognition"]["condition"] == "2 + 2"
    assert updated["preliminary"]["status"] == "correct"
    assert harness.subscription(auth)["checks_remaining"] == 0


class FlakyProvider(FakeAIProvider):
    def __init__(self, *, retryable):
        super().__init__()
        self.retryable = retryable
        self.calls = 0

    def recognize(self, pages, clarification):
        self.calls += 1
        if self.calls == 1:
            raise AIError("provider_unavailable", retryable=self.retryable)
        return super().recognize(pages, clarification)


def test_transient_ai_failure_retries_same_reservation_and_duplicate_is_safe(build_harness):
    provider = FlakyProvider(retryable=True)
    harness = build_harness(ai_provider=provider)
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    job_id = submission["job_id"]

    harness.process(job_id)
    assert harness.submission(auth, submission["id"])["status"] == "queued"
    assert harness.subscription(auth)["checks_remaining"] == 0
    harness.process(job_id)
    harness.process(job_id)
    assert provider.calls == 2
    assert harness.submission(auth, submission["id"])["status"] == "completed"
    assert len(harness.results(auth, submission["id"])["tasks"]) == 1
    assert harness.subscription(auth)["checks_remaining"] == 0


def test_single_attempt_stops_automatic_resends_and_releases_reservation(build_harness):
    provider = FlakyProvider(retryable=True)
    harness = build_harness(ai_provider=provider, max_attempts=1)
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission['job_id'])
    harness.process(submission['job_id'])
    assert provider.calls == 1
    assert harness.submission(auth, submission['id'])['status'] == 'failed'
    assert harness.subscription(auth)['checks_remaining'] == 1


def test_terminal_ai_failure_refunds_reservation_and_exposes_stage(build_harness):
    provider = FlakyProvider(retryable=False)
    harness = build_harness(ai_provider=provider)
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    failed = harness.submission(auth, submission["id"])
    assert failed["status"] == "failed"
    assert failed["stage"]
    assert failed["error_code"] == "provider_unavailable"
    assert harness.subscription(auth)["checks_remaining"] == 1
    harness.process(submission["job_id"])
    assert provider.calls == 1
    retry = harness.client.post(f"/v1/submissions/{submission['id']}/retry", headers=auth)
    assert retry.status_code == 202, retry.text
    assert retry.json()["job_id"] != submission["job_id"]
    harness.process(retry.json()["job_id"])
    assert harness.submission(auth, submission["id"])["status"] == "completed"
    assert harness.subscription(auth)["checks_remaining"] == 0


def test_database_scan_recovers_expired_worker_lease_without_queue_signal(harness):
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    job_id = submission["job_id"]
    state = harness.app.state
    assert state.queue.pop(timeout=0) == job_id
    assert state.queue.pop(timeout=0) is None
    assert job_id in due_jobs(state)

    # Simulate a worker crash after committing the claim and consuming the signal.
    with state.session_factory.begin() as db:
        job = db.get(ProcessingJob, job_id)
        job.status = "processing"
        job.attempts = 1
        job.lease_token = new_id()
        job.lease_until = utcnow() - timedelta(seconds=1)
        db.get(Submission, submission["id"]).status = "processing"

    assert job_id in due_jobs(state)
    harness.process(job_id)
    assert job_id not in due_jobs(state)
    job_status = harness.client.get(f"/v1/submissions/{submission['id']}/jobs/{job_id}", headers=auth)
    assert job_status.status_code == 200
    assert job_status.json().items() >= {"status": "completed", "attempts": 2}.items()
    assert harness.subscription(auth)["checks_remaining"] == 0


def test_duplicate_delivery_cannot_claim_active_worker_lease(build_harness):
    started = Event()
    resume = Event()

    class BlockedProvider(FakeAIProvider):
        calls = 0

        def recognize(self, pages, clarification):
            self.calls += 1
            started.set()
            if not resume.wait(timeout=10):
                raise RuntimeError("test did not release the active provider")
            return super().recognize(pages, clarification)

    provider = BlockedProvider()
    harness = build_harness(ai_provider=provider)
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    job_id = submission["job_id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_delivery = executor.submit(harness.process, job_id)
        try:
            assert started.wait(timeout=5)
            assert harness.submission(auth, submission["id"])["status"] == "processing"
            duplicate_delivery = executor.submit(harness.process, job_id)
            duplicate_delivery.result(timeout=5)
            assert provider.calls == 1
        finally:
            resume.set()
        first_delivery.result(timeout=5)

    assert harness.submission(auth, submission["id"])["status"] == "completed"
    assert harness.subscription(auth)["checks_remaining"] == 0


def test_repeat_delete_resumes_partial_object_cleanup(harness, monkeypatch):
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    path = f"/v1/submissions/{submission['id']}"
    storage = harness.app.state.storage
    delete = storage.delete
    calls = 0

    def fail_once(key):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated transient object storage failure")
        delete(key)

    monkeypatch.setattr(storage, "delete", fail_once)
    failed = harness.client.delete(path, headers=auth)
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "deletion_pending"
    assert harness.client.get(path, headers=auth).status_code == 404
    assert storage.objects

    repeated = harness.client.delete(path, headers=auth)
    assert repeated.status_code == 204, repeated.text
    assert not storage.objects
    assert harness.subscription(auth)["checks_remaining"] == 0

import json
import logging

import httpx
import pytest

from app.ai import (
    AIError,
    FakeAIProvider,
    GeminiAIProvider,
    PageInput,
    PreliminaryReview,
    RecognizedTask,
    validate_recognition,
    validate_reviews,
)
from app.ai.formal import formal_check


def task(condition, answer, kind="arithmetic", state="present", **extra):
    return RecognizedTask(
        page_number=1, condition=condition, answer=answer, kind=kind,
        answer_state=state, **extra,
    )


@pytest.mark.parametrize(
    ("condition", "answer", "kind", "expected"),
    [
        ("2 + 2", "5", "arithmetic", (False, "4")),
        ("2 + 2", "4", "unknown", (True, "4")),
        ("Сравни -2 ? -3", ">", "unknown", (True, ">")),
        ("Вычисли: -2 × (3 + 1,5) =", "-9", "arithmetic", (True, "-9")),
        ("0,1 + 0,2", "0.3", "arithmetic", (True, "0.3")),
        ("1/3", "0.3", "arithmetic", None),
        ("5/0", "0", "arithmetic", None),
        ("1 + __import__('os')", "1", "arithmetic", None),
        ("Сравни 2,0 ? 2", "=", "numeric_comparison", (True, "=")),
        ("Сравни 2,0 и 1.5", ">", "numeric_comparison", (True, ">")),
        ("Переведи 1 м в см", "100 см", "measurement", (True, "100 см")),
        ("1 kg = ? g", "1000", "measurement", (True, "1000 g")),
        ("1 kg = ? m", "1000", "measurement", None),
        ("1 кг ? 999 г", ">", "measurement", (True, ">")),
        ("1 m ? 100 cm", "=", "measurement", (True, "=")),
    ],
)
def test_formal_grammar(condition, answer, kind, expected):
    assert formal_check(task(condition, answer, kind)) == expected


@pytest.mark.parametrize(
    ("recognized", "status"),
    [
        (task(None, "5"), "needs_review"),
        (task("2+2", None, state="unknown"), "needs_review"),
        (task("2+2", None, state="blank"), "no_answer"),
        (task(None, None, state="blank"), "needs_review"),
        (task("2+2", None, state="unreadable"), "unreadable"),
        (task("2+2", "5", ambiguity="Две трактовки"), "needs_review"),
        (task("Напиши сочинение", "Текст сочинения", "open"), "needs_review"),
        (task("Сложное условие", "Ответ", "unknown"), "needs_review"),
    ],
)
def test_server_guardrails_override_model_claim(recognized, status):
    confident_claim = PreliminaryReview(
        status="correct", explanation="Утверждение модели", expected_answer="42"
    )
    review, = validate_reviews([recognized], [confident_claim])
    assert review.status == status
    assert review.expected_answer is None
    assert validate_reviews([recognized], [review]) == [review]


def test_source_and_cardinality_are_validated():
    page = PageInput(3, b"source", "image/jpeg")
    found = task("2 + 2", "5")
    with pytest.raises(AIError, match="ai_invalid_source_page"):
        validate_recognition([found], [page])
    with pytest.raises(AIError, match="ai_invalid_review_count"):
        validate_reviews([found], [])


def test_fake_uses_fictional_tasks_and_never_reads_page():
    fake = FakeAIProvider()
    left = fake.recognize([PageInput(1, b"first-image", "image/jpeg")], None)
    right = fake.recognize([PageInput(1, b"second-image", "image/jpeg")], None)
    assert fake.demonstration is True
    assert left == right
    assert fake.review(left, None)[0].expected_answer == "4"


def google_response(payload):
    return httpx.Response(
        200,
        json={
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [
                    {"thought": True, "text": "INTERNAL_GOOGLE_THOUGHT"},
                    {"text": json.dumps(payload)},
                ]},
            }],
            "usageMetadata": {"totalTokenCount": 150},
        },
    )


def test_gemini_uses_documented_structured_requests_and_safe_logs(monkeypatch, caplog):
    provider = GeminiAIProvider("PRIVATE_API_KEY")
    page = PageInput(1, b"PRIVATE_IMAGE_BYTES", "image/jpeg")
    requests = []
    responses = iter([
        google_response({"tasks": [
            {"page_number": 1, "answer_state": "present", "answer": "5",
             "condition": "2+2", "kind": "arithmetic"},
            {"page_number": 1, "answer_state": "present", "answer": "PRIVATE_STUDENT_TEXT",
             "condition": "Сочинение", "kind": "open"},
            {"page_number": 1, "answer_state": "present", "answer": "Тихий",
             "condition": "Самый большой океан Земли", "kind": "closed"},
        ]}),
        google_response({"reviews": [
            {"index": 2, "status": "correct", "explanation": "Ответ совпадает"},
            {"index": 1, "status": "needs_review", "explanation": "Критерии нужны"},
        ]}),
    ])

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr("app.ai.gemini.httpx.post", post)
    with caplog.at_level(logging.INFO, logger="stopkadr"):
        found = provider.recognize([page], "PRIVATE_TEACHER_NOTE")
        checked = provider.review(found, "PRIVATE_TEACHER_NOTE")

    assert [review.status for review in checked] == ["incorrect", "needs_review", "correct"]
    assert checked[0].expected_answer == "4"
    for (url, request), expected_root in zip(requests, ["tasks", "reviews"], strict=True):
        assert url == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.8-flash:generateContent"
        )
        assert request["headers"] == {"x-goog-api-key": "PRIVATE_API_KEY"}
        body = request["json"]
        assert "недоверенные данные" in body["systemInstruction"]["parts"][0]["text"]
        config = body["generationConfig"]["responseFormat"]["text"]
        assert config["mimeType"] == "APPLICATION_JSON"
        assert config["schema"]["required"] == [expected_root]
        serialized = json.dumps(config["schema"])
        for unsupported in ("minLength", "maxLength", "exclusiveMaximum", "default"):
            assert f'"{unsupported}":' not in serialized

    recognition_request, review_request = (request["json"] for _, request in requests)
    assert recognition_request["contents"][0]["parts"][-1]["inlineData"] == {
        "mimeType": "image/jpeg",
        "data": "UFJJVkFURV9JTUFHRV9CWVRFUw==",
    }
    assert not any("inlineData" in part for part in review_request["contents"][0]["parts"])
    review_input = json.loads(review_request["contents"][0]["parts"][0]["text"])
    assert [(item["index"], item["kind"]) for item in review_input["tasks"]] == [
        (1, "open"), (2, "closed"),
    ]
    events = [record.event_fields for record in caplog.records if record.msg == "ai_call"]
    assert [(event["stage"], event["total_tokens"]) for event in events] == [
        ("recognize", 150), ("review", 150),
    ]
    assert "PRIVATE_" not in caplog.text and "PRIVATE_" not in str(events)
    assert "INTERNAL_GOOGLE_THOUGHT" not in caplog.text and "INTERNAL_GOOGLE_THOUGHT" not in str(events)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (429, "ai_unavailable", True),
        (503, "ai_unavailable", True),
        (403, "ai_request_rejected", False),
        (200, "ai_invalid_response", False),
    ],
)
def test_gemini_error_codes_do_not_expose_response(monkeypatch, status, code, retryable):
    monkeypatch.setattr(
        "app.ai.gemini.httpx.post",
        lambda *args, **kwargs: httpx.Response(status, text="PRIVATE_RESPONSE"),
    )
    with pytest.raises(AIError, match=f"^{code}$") as error:
        GeminiAIProvider("key").recognize([PageInput(1, b"page", "image/jpeg")], None)
    assert error.value.retryable is retryable


def test_gemini_rechecks_deterministic_answers_without_external_calls(monkeypatch):
    def unavailable(*args, **kwargs):
        pytest.fail("A deterministic recheck must not send an external request")

    monkeypatch.setattr("app.ai.gemini.httpx.post", unavailable)
    provider = GeminiAIProvider("key")
    reviews = provider.review([
        task("2 + 2", "4", "unknown"),
        task("2 + 2", None, state="blank"),
        task(None, "4"),
    ], None)
    assert [review.status for review in reviews] == ["correct", "no_answer", "needs_review"]


@pytest.mark.parametrize("response", [
    {"promptFeedback": {"blockReason": "SAFETY"}},
    {"candidates": [{"finishReason": "SAFETY"}]},
    {"candidates": [{"finishReason": "SPII"}]},
])
def test_gemini_content_block_has_safe_terminal_error(monkeypatch, response):
    monkeypatch.setattr(
        "app.ai.gemini.httpx.post", lambda *args, **kwargs: httpx.Response(200, json=response)
    )
    with pytest.raises(AIError, match="^ai_content_blocked$") as error:
        GeminiAIProvider("key").recognize([PageInput(1, b"page", "image/jpeg")], None)
    assert error.value.retryable is False


def test_gemini_timeout_is_retryable_without_leaking_error_details(monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("private request details")

    monkeypatch.setattr("app.ai.gemini.httpx.post", timeout)
    with pytest.raises(AIError, match="^ai_unavailable$") as error:
        GeminiAIProvider("key").recognize([PageInput(1, b"page", "image/jpeg")], None)
    assert error.value.retryable is True


def test_teacher_condition_change_does_not_grade_with_stale_recognition_kind(build_harness, monkeypatch):
    requests = []
    responses = iter([
        google_response({"tasks": [{
            "page_number": 1, "answer_state": "present", "answer": "Тихий океан",
            "condition": "Назови самый большой океан", "kind": "closed",
        }]}),
        google_response({"reviews": [{
            "index": 1, "status": "correct", "explanation": "Ответ совпадает",
        }]}),
        google_response({"reviews": [{
            "index": 1, "status": "correct", "explanation": "Недоказанная оценка",
        }]}),
    ])

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr("app.ai.gemini.httpx.post", post)
    harness = build_harness(ai_provider=GeminiAIProvider("fixture-key"))
    auth, _ = harness.register()
    harness.subscribe(auth)
    submission = harness.queued_submission(auth)
    harness.process(submission["job_id"])
    original, = harness.results(auth, submission["id"])["tasks"]
    assert original["preliminary"]["status"] == "correct"
    path = f"/v1/submissions/{submission['id']}/tasks/{original['id']}"
    correction = harness.client.patch(
        path, headers=auth, json={"condition": "Объясни, как океаны влияют на климат"}
    )
    assert correction.status_code == 200
    recheck = harness.client.post(f"{path}/recheck", headers=auth)
    assert recheck.status_code == 202
    harness.process(recheck.json()["job_id"])

    updated, = harness.results(auth, submission["id"])["tasks"]
    assert updated["recognition"] == original["recognition"]
    assert updated["preliminary"]["status"] == "needs_review"
    review_input = json.loads(requests[2]["contents"][0]["parts"][0]["text"])
    assert review_input["tasks"][0]["kind"] == "unknown"
    assert harness.subscription(auth)["checks_remaining"] == 0


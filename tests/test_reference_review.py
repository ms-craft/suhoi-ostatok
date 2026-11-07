import json

from app.ai import GeminiAIProvider, RecognizedTask
from types import SimpleNamespace
from app import work


def test_reference_review_sends_teacher_answer_to_review_model(monkeypatch):
    captured = {}

    class Reply:
        status_code = 200

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "reviews": [{"index": 1, "status": "incorrect", "explanation": "Сумма равна 4.",
                             "expected_answer": "4", "uncertainty": None, "missing_data": []}]
            })}}]}

    def post(url, **kwargs):
        captured.update(kwargs["json"])
        return Reply()

    monkeypatch.setattr("app.ai.gemini.httpx.post", post)
    provider = GeminiAIProvider("key", "gemini-3.8-flash", "https://example.test/v1", "gemini-3.5-flash-lite")
    task = RecognizedTask(page_number=1, number="1", condition="2+2", answer="5", answer_state="present", kind="arithmetic")
    result = provider.review_reference(task, {"question": "2+2", "answer": "4"}, None)
    assert captured["model"] == "gemini-3.8-flash"
    assert json.loads(captured["messages"][1]["content"])["teacher_answer"] == "4"
    assert result.status == "incorrect"


def test_unclear_answer_is_not_compared_by_model(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("ambiguous answer should not be sent for comparison")
    monkeypatch.setattr("app.ai.gemini.httpx.post", fail)
    provider = GeminiAIProvider("key")
    task = RecognizedTask(page_number=1, number="1", answer="5", answer_state="present", ambiguity="Цифра неразборчива")
    result = provider.review_reference(task, {"question": "2+2", "answer": "4"}, None)
    assert result.status == "needs_review"


def test_reference_match_skips_model_and_mismatch_keeps_teacher_answer(monkeypatch):
    from app.ai import PreliminaryReview
    class Provider:
        def __init__(self): self.calls = 0
        def review(self, *args): raise AssertionError("ordinary review must not run with a test")
        def review_reference(self, *args):
            self.calls += 1
            return PreliminaryReview(status="incorrect", explanation="Ошибка", expected_answer="придуманный ответ")
    provider = Provider()
    tasks = [RecognizedTask(page_number=1, number="1", answer="КОМАР", answer_state="present"),
             RecognizedTask(page_number=1, number="2", answer="камар", answer_state="present")]
    refs = [{"question": "Слово 1", "answer": "комар"}, {"question": "Слово 2", "answer": "комар"}]
    monkeypatch.setattr(work, "_load_inputs", lambda *a: ([], [], None, refs))
    monkeypatch.setattr(work, "_source_pages", lambda *a: [])
    monkeypatch.setattr(work, "_stage", lambda *a: None)
    monkeypatch.setattr(work, "validate_recognition", lambda *a: tasks)
    monkeypatch.setattr(work, "_match_tasks", lambda *a: [None, None])
    monkeypatch.setattr(provider, "recognize", lambda *a: tasks, raising=False)
    result = work._analyze(SimpleNamespace(ai_provider=provider), SimpleNamespace(kind="initial"))
    assert provider.calls == 1
    assert result[0].review.status == "correct"
    assert result[1].review.expected_answer == "комар"
    assert result[1].review.expected_answer_source == "teacher_test"


def test_reference_review_uses_teacher_condition_and_complete_solution(monkeypatch):
    captured = {}
    class Reply:
        status_code = 200
        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "reviews": [{"index": 1, "status": "correct", "explanation": "Корни совпадают.",
                             "expected_answer": "-1, 3", "uncertainty": None, "missing_data": []}]
            })}}]}
    def post(url, **kwargs):
        captured.update(kwargs["json"])
        return Reply()
    monkeypatch.setattr("app.ai.gemini.httpx.post", post)
    provider = GeminiAIProvider("key", "gemini-3.8-flash", "https://example.test/v1")
    task = RecognizedTask(page_number=1, number="1", answer="По теореме Виета: x1*x2=-3; x1+x2=2; x1=-1; x2=3. Ответ: -1; 3", answer_state="present")
    result = provider.review_reference(task, {"question": "Решите x²-2x-3=0", "answer": "-1, 3"}, None)
    assert result.status == "correct"
    assert "используй условие учителя" in captured["messages"][0]["content"]
    assert json.loads(captured["messages"][1]["content"])["task"]["answer"] == task.answer


def test_recognition_returns_name_even_when_no_tasks(monkeypatch):
    from app.ai import PageInput
    class Reply:
        status_code = 200
        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "student_name": "Иван Иванов", "tasks": []
            })}}]}
    monkeypatch.setattr("app.ai.gemini.httpx.post", lambda *args, **kwargs: Reply())
    provider = GeminiAIProvider("key", "gemini-3.8-flash", "https://example.test/v1")
    tasks, name = provider.recognize_with_name([PageInput(1, b"image", "image/jpeg")], None)
    assert tasks == []
    assert name == "Иван Иванов"

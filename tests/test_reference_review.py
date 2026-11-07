import json
import pytest

from app.ai import GeminiAIProvider, RecognizedTask
from types import SimpleNamespace
from app import work


@pytest.mark.parametrize(('kind', 'answer', 'numbers', 'references', 'only_task', 'status', 'calls'), [
    ('initial', 'Друг, член, комар, муха.', [None], 1, True, 'correct', 0),
    ('initial', 'Друг, член, камар, муха.', [None], 1, True, 'incorrect', 1),
    ('initial', 'Друг, член, камар, муха.', [None], 2, True, 'needs_review', 0),
    ('initial', 'Друг, член, камар, муха.', [None, None], 1, True, 'needs_review', 0),
    ('initial', 'Друг, член, камар, муха.', ['2'], 1, True, 'needs_review', 0),
    ('recheck', 'Друг, член, камар, муха.', [None], 1, True, 'incorrect', 1),
    ('recheck', 'Друг, член, камар, муха.', [None], 1, False, 'needs_review', 0),
])
def test_unnumbered_dictation_matches_only_unambiguous_reference(
        monkeypatch, kind, answer, numbers, references, only_task, status, calls):
    from app.ai import PreliminaryReview

    class Provider:
        def __init__(self):
            self.calls = []

        def recognize(self, *args):
            return tasks

        def review_reference(self, task, reference, clarification):
            self.calls.append((task, reference))
            return PreliminaryReview(status='incorrect', explanation='камар → комар', expected_answer='выдуманный ответ')

    provider = Provider()
    tasks = [RecognizedTask(page_number=1, number=n, answer=answer, answer_state='present', kind='open') for n in numbers]
    refs = [{'question': 'Диктант', 'answer': 'Друг, член, комар, муха.'}] * references
    previous = [work.InputTask(str(i), task.model_dump(), None) for i, task in enumerate(tasks)] if kind == 'recheck' else []
    monkeypatch.setattr(work, '_load_inputs', lambda *args: ([], previous, None, refs, 'russian', 'humanities'))
    monkeypatch.setattr(work, '_source_pages', lambda *args: [])
    monkeypatch.setattr(work, '_stage', lambda *args: None)
    monkeypatch.setattr(work, 'validate_recognition', lambda *args: tasks)
    monkeypatch.setattr(work, '_submission_has_one_task', lambda *args: only_task)
    result = work._analyze(SimpleNamespace(ai_provider=provider), SimpleNamespace(kind=kind))
    assert result[0].review.status == status
    assert len(provider.calls) == calls
    if status != 'needs_review':
        assert result[0].review.expected_answer == refs[0]['answer']
        assert result[0].review.expected_answer_source == 'teacher_test'
    if calls:
        assert provider.calls[0][1] == refs[0]


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


def test_missing_answer_is_not_compared_by_model(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("ambiguous answer should not be sent for comparison")
    monkeypatch.setattr("app.ai.gemini.httpx.post", fail)
    provider = GeminiAIProvider("key")
    task = RecognizedTask(page_number=1, number="1", answer_state="unreadable", ambiguity="Цифра неразборчива")
    result = provider.review_reference(task, {"question": "2+2", "answer": "4"}, None)
    assert result.status == "needs_review"


def test_reference_provider_sends_ambiguous_transcription(monkeypatch):
    captured = {}
    class Reply:
        status_code = 200
        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "reviews": [{"index": 1, "status": "needs_review", "explanation": "паёт вместо поёт",
                             "uncertainty": "Буква неразборчива", "missing_data": []}]
            })}}]}
    def post(url, **kwargs):
        captured.update(kwargs['json'])
        return Reply()
    monkeypatch.setattr('app.ai.gemini.httpx.post', post)
    provider = GeminiAIProvider('key', api_base='https://example.test/v1')
    task = RecognizedTask(page_number=1, answer='жэвёт и паёт', answer_state='present', ambiguity='Буква неразборчива')
    result = provider.review_reference(task, {'question': 'Диктант', 'answer': 'живёт и поёт'}, None)
    payload = json.loads(captured['messages'][1]['content'])
    assert payload['task']['ambiguity'] == 'Буква неразборчива'
    assert payload['teacher_answer'] == 'живёт и поёт'
    assert 'паёт' in result.explanation


def test_reference_review_compares_transcription_despite_ambiguity(monkeypatch):
    from app.ai import PreliminaryReview
    class Provider:
        def review_reference(self, task, reference, clarification):
            assert task.ambiguity == 'Буква неразборчива'
            assert reference['answer'] == 'живёт и поёт'
            return PreliminaryReview(status='incorrect', explanation='жэвёт вместо живёт; паёт вместо поёт')

    task = RecognizedTask(page_number=1, number=None, answer='жэвёт и паёт', answer_state='present', ambiguity='Буква неразборчива')
    previous = [work.InputTask('task', task.model_dump(), None)]
    monkeypatch.setattr(work, '_load_inputs', lambda *a: ([], previous, None, [{'question': 'Диктант', 'answer': 'живёт и поёт'}], 'russian', 'humanities'))
    monkeypatch.setattr(work, '_submission_has_one_task', lambda *a: True)
    monkeypatch.setattr(work, '_stage', lambda *a: None)
    result = work._analyze(SimpleNamespace(ai_provider=Provider()), SimpleNamespace(kind='recheck'))[0].review
    assert result.status == 'incorrect'
    assert 'паёт вместо поёт' in result.explanation
    assert result.uncertainty == 'Буква неразборчива'
    assert result.expected_answer == 'живёт и поёт'


@pytest.mark.parametrize(('model_status', 'expected_status'), [
    ('correct', 'needs_review'),
    ('needs_review', 'needs_review'),
    ('incorrect', 'incorrect'),
])
def test_ambiguity_preserves_model_error_status(monkeypatch, model_status, expected_status):
    from app.ai import PreliminaryReview
    class Provider:
        def review_reference(self, task, reference, clarification):
            return PreliminaryReview(status=model_status, explanation='Проверка ответа')

    task = RecognizedTask(page_number=1, answer='Друк, комор', answer_state='present',
                          ambiguity='Слово «комор» может быть прочитано как «комар».')
    previous = [work.InputTask('task', task.model_dump(), None)]
    refs = [{'question': 'Диктант', 'answer': 'Друг, комар'}]
    monkeypatch.setattr(work, '_load_inputs', lambda *a: ([], previous, None, refs, 'russian', 'humanities'))
    monkeypatch.setattr(work, '_submission_has_one_task', lambda *a: True)
    monkeypatch.setattr(work, '_stage', lambda *a: None)
    result = work._analyze(SimpleNamespace(ai_provider=Provider()), SimpleNamespace(kind='recheck'))[0].review
    assert result.status == expected_status
    assert result.uncertainty == task.ambiguity


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
    monkeypatch.setattr(work, "_load_inputs", lambda *a: ([], [], None, refs, None, None))
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


def test_corrected_number_selects_the_matching_teacher_answer(build_harness):
    from app.ai import FakeAIProvider, PreliminaryReview
    from tests.support import photograph

    class Provider(FakeAIProvider):
        def recognize(self, pages, clarification):
            return [RecognizedTask(page_number=1, number='1', answer='2', answer_state='present')]
        def review_reference(self, task, reference, clarification):
            return PreliminaryReview(status='incorrect', explanation='Ответ не совпадает.')

    harness = build_harness(ai_provider=Provider(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    refs = [{'question': 'Первое', 'answer': '1'}, {'question': 'Второе', 'answer': '2'}]
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'test_questions': json.dumps(refs)})
    assert response.status_code == 202
    submission = response.json()
    harness.process(submission['job_id'])
    task, = harness.results(auth, submission['id'])['tasks']
    path = f"/v1/submissions/{submission['id']}/tasks/{task['id']}"
    assert task['preliminary']['expected_answer'] == '1'
    assert harness.client.patch(path, headers=auth, json={'number': '0'}).status_code == 422
    edited = harness.client.patch(path, headers=auth, json={'number': '2'})
    assert edited.status_code == 200, edited.text
    assert edited.json()['teacher']['number'] == '2'
    assert edited.json()['recognition']['number'] == '1'
    assert edited.json()['preliminary']['status'] == 'needs_review'
    assert edited.json()['preliminary']['expected_answer'] is None
    recheck = harness.client.post(path + '/recheck', headers=auth)
    assert recheck.status_code == 202
    harness.process(recheck.json()['job_id'])
    result, = harness.results(auth, submission['id'])['tasks']
    assert result['recognition']['number'] == '1'
    assert result['teacher']['number'] == '2'
    assert result['preliminary']['status'] == 'correct'
    assert result['preliminary']['expected_answer'] == '2'


def test_recheck_snapshots_latest_linked_teacher_work(build_harness):
    from app.ai import FakeAIProvider, PreliminaryReview
    from tests.support import photograph

    class Provider(FakeAIProvider):
        def recognize(self, pages, clarification):
            return [RecognizedTask(page_number=1, number='1', answer='новый', answer_state='present')]
        def review_reference(self, task, reference, clarification):
            return PreliminaryReview(status='incorrect', explanation='Ответ отличается.')

    harness = build_harness(ai_provider=Provider(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    original = [{'question': 'Вопрос', 'answer': 'старый'}]
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'test_questions': json.dumps(original, ensure_ascii=False)})
    submission = response.json()
    harness.process(submission['job_id'])
    task, = harness.results(auth, submission['id'])['tasks']
    assert task['preliminary']['expected_answer'] == 'старый'
    data = {'classes': [], 'tests': [{'id': 'work1', 'name': 'Работа',
                                     'questions': [{'question': 'Вопрос', 'answer': 'новый'}]}],
            'links': {submission['id']: {'testId': 'work1'}}}
    assert harness.client.put('/v1/workspace', headers=auth, json={'revision': 0, 'workspace': data}).status_code == 200
    path = f"/v1/submissions/{submission['id']}/tasks/{task['id']}/recheck"
    recheck = harness.client.post(path, headers=auth)
    assert recheck.status_code == 202, recheck.text
    # Later edits must not silently alter an already queued check.
    data['tests'][0]['questions'][0]['answer'] = 'ещё новее'
    assert harness.client.put('/v1/workspace', headers=auth, json={'revision': 1, 'workspace': data}).status_code == 200
    harness.process(recheck.json()['job_id'])
    result, = harness.results(auth, submission['id'])['tasks']
    assert result['preliminary']['expected_answer'] == 'новый'
    assert result['preliminary']['status'] == 'correct'
    recheck = harness.client.post(path, headers=auth)
    assert recheck.status_code == 202
    harness.process(recheck.json()['job_id'])
    result, = harness.results(auth, submission['id'])['tasks']
    assert result['preliminary']['expected_answer'] == 'ещё новее'
    assert result['preliminary']['status'] == 'incorrect'


def test_recheck_rejects_missing_linked_teacher_work(build_harness):
    from app.ai import FakeAIProvider
    from tests.support import photograph
    harness = build_harness(ai_provider=FakeAIProvider(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    submission = harness.client.post('/v1/submissions', headers=auth, files=[photograph()]).json()
    harness.process(submission['job_id'])
    task, = harness.results(auth, submission['id'])['tasks']
    workspace = {'classes': [], 'tests': [], 'links': {submission['id']: {'testId': 'deleted'}}}
    assert harness.client.put('/v1/workspace', headers=auth, json={'revision': 0, 'workspace': workspace}).status_code == 200
    result = harness.client.post(f"/v1/submissions/{submission['id']}/tasks/{task['id']}/recheck", headers=auth)
    assert result.status_code == 409
    assert harness.submission(auth, submission['id'])['job_id'] == submission['job_id']


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

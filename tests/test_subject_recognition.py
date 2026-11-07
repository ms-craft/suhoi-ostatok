import json
from types import SimpleNamespace

import httpx
import pytest

from app.ai import GeminiAIProvider, PageInput
from app.work import _load_inputs
from tests.support import photograph


@pytest.mark.parametrize(('subject', 'expected'), [
    ('math', 'gemini-3.5-flash-lite'),
    ('russian', 'gemini-3.8-flash'),
    ('history', 'gemini-3.8-flash'),
    (None, 'gemini-3.5-flash-lite'),
])
@pytest.mark.parametrize('openai_compatible', [True, False])
def test_subject_selects_recognition_model(monkeypatch, subject, expected, openai_compatible):
    requests = []
    def post(url, **kwargs):
        requests.append((url, kwargs))
        if openai_compatible:
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"tasks": []}'}}]})
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"tasks": []}'}]}}]})
    monkeypatch.setattr('app.ai.gemini.httpx.post', post)
    endpoint = 'https://example.test/v1' if openai_compatible else 'https://generativelanguage.googleapis.com/v1beta/models'
    provider = GeminiAIProvider('key', api_base=endpoint, recognition_model='gemini-3.5-flash-lite')
    provider.recognize_with_name([PageInput(1, b'fake-image', 'image/jpeg')], None, subject=subject)
    url, request = requests[0]
    assert request['json']['model'] == expected if openai_compatible else f'/{expected}:generateContent' in url


@pytest.mark.parametrize('openai_compatible', [True, False])
def test_recognition_sends_strikethrough_instructions(monkeypatch, openai_compatible):
    captured = []

    def post(url, **kwargs):
        captured.append(kwargs['json'])
        if openai_compatible:
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"tasks": []}'}}]})
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"tasks": []}'}]}}]})

    monkeypatch.setattr('app.ai.gemini.httpx.post', post)
    endpoint = 'https://example.test/v1' if openai_compatible else 'https://generativelanguage.googleapis.com/v1beta/models'
    provider = GeminiAIProvider('key', api_base=endpoint)
    provider.recognize_with_name([PageInput(1, b'image', 'image/jpeg')], None, subject='russian')
    payload = captured[0]
    instruction = payload['messages'][0]['content'] if openai_compatible else payload['systemInstruction']['parts'][0]['text']
    assert 'вставь исправление в соответствующее место ответа' in instruction
    assert 'Зачёркнутые буквы' in instruction
    assert 'итоговая запись — «жэвёт»' in instruction
    assert 'Сам факт исправления не является неопределённостью' in instruction
    assert 'в corrections' in instruction
    assert 'не записывай сам факт зачёркивания или исправления в ambiguity' in instruction
    assert 'не заменяй\nэто число на «2»' in instruction


@pytest.mark.parametrize(('answer', 'status'), [('живёт', 'correct'), ('жэвёт', 'incorrect')])
def test_corrections_are_saved_without_blocking_reference_review(build_harness, answer, status):
    from app.ai import FakeAIProvider, PreliminaryReview, RecognizedTask

    class Provider(FakeAIProvider):
        def recognize_with_name(self, pages, clarification, subject=None, subject_category=None):
            return [RecognizedTask(page_number=1, answer=answer, answer_state='present', kind='open',
                                   corrections=['В слове исправлена буква'])], None

        def review_reference(self, task, reference, clarification):
            assert task.corrections == ['В слове исправлена буква']
            assert reference['answer'] == 'живёт'
            return PreliminaryReview(status='incorrect', explanation='жэвёт вместо живёт')

    harness = build_harness(ai_provider=Provider())
    auth, _ = harness.register()
    harness.subscribe(auth)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'test_subject': 'russian', 'test_questions': json.dumps([{'question': 'Диктант', 'answer': 'живёт'}])})
    assert response.status_code == 202, response.text
    submission = response.json()
    harness.process(submission['job_id'])
    task, = harness.results(auth, submission['id'])['tasks']
    assert task['recognition']['corrections'] == ['В слове исправлена буква']
    assert task['recognition']['ambiguity'] is None
    assert task['preliminary']['status'] == status
    assert task['preliminary']['expected_answer'] == 'живёт'


def test_subject_is_saved_with_submission_and_old_snapshots_work(harness):
    auth, _ = harness.register()
    harness.subscribe(auth)
    questions = [{'question': 'Напишите слово', 'answer': 'комар'}]
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'test_subject': 'russian', 'test_questions': json.dumps(questions)})
    assert response.status_code == 202, response.text
    submission = response.json()
    claim = SimpleNamespace(submission_id=submission['id'], kind='initial', task_id=None)
    _, _, _, refs, subject, category = _load_inputs(harness.app.state, claim)
    assert refs == questions
    assert subject == 'russian'
    assert category == 'humanities'
    from app.submission_models import ProcessingJob
    with harness.app.state.session_factory.begin() as db:
        job = db.get(ProcessingJob, submission['job_id'])
        job.clarification = json.dumps(questions)
    _, _, _, refs, subject, category = _load_inputs(harness.app.state, claim)
    assert refs == questions
    assert subject is None
    assert category is None


def test_worker_passes_saved_subject_to_recognition(build_harness):
    from app.ai import FakeAIProvider

    class SubjectProvider(FakeAIProvider):
        subject = None
        def recognize_with_name(self, pages, clarification, subject=None, subject_category=None):
            self.subject = subject
            self.subject_category = subject_category
            return [], None

    provider = SubjectProvider()
    harness = build_harness(ai_provider=provider)
    auth, _ = harness.register()
    harness.subscribe(auth)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'test_subject': 'history'})
    assert response.status_code == 202
    submission = response.json()
    harness.process(submission['job_id'])
    assert provider.subject == 'history'
    assert provider.subject_category == 'humanities'
    assert harness.submission(auth, submission['id'])['status'] == 'completed'


def test_catalog_and_custom_subjects(harness):
    from app.subjects import Subject
    with harness.app.state.session_factory.begin() as db:
        db.add_all([Subject(name='Физика', category='stem'), Subject(name='Химия', category='stem'), Subject(name='Русский язык', category='humanities')])
    auth, _ = harness.register()
    assert harness.client.get('/v1/subjects').status_code == 401
    suggestions = harness.client.get('/v1/subjects', headers=auth, params={'q': 'ФИЗ'}).json()
    assert suggestions == [{'name': 'Физика', 'category': 'stem'}]
    harness.subscribe(auth)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'test_subject': 'физика', 'test_subject_category': 'humanities'})
    assert response.status_code == 202
    claim = SimpleNamespace(submission_id=response.json()['id'], kind='initial', task_id=None)
    assert _load_inputs(harness.app.state, claim)[-2:] == ('физика', 'stem')


def test_custom_subject_uses_advanced_model_automatically(harness):
    auth, _ = harness.register()
    harness.subscribe(auth)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()], data={'test_subject': 'Краеведение'})
    assert response.status_code == 202
    claim = SimpleNamespace(submission_id=response.json()['id'], kind='initial', task_id=None)
    assert _load_inputs(harness.app.state, claim)[-2:] == ('Краеведение', 'humanities')


def test_custom_humanities_uses_advanced_model(monkeypatch):
    captured = []
    def post(url, **kwargs):
        captured.append(kwargs['json'])
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"tasks": []}'}}]})
    monkeypatch.setattr('app.ai.gemini.httpx.post', post)
    provider = GeminiAIProvider('key', api_base='https://example.test/v1', recognition_model='gemini-3.5-flash-lite')
    provider.recognize_with_name([PageInput(1, b'img', 'image/jpeg')], None, subject='Краеведение', subject_category='humanities')
    assert captured[0]['model'] == 'gemini-3.8-flash'
    assert any('Краеведение' in part.get('text', '') for part in captured[0]['messages'][1]['content'])

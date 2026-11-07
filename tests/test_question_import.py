import json

import httpx
import pytest

from app.ai import AIError, GeminiAIProvider, PageInput
from tests.support import photograph


@pytest.mark.parametrize('compatible', [True, False])
def test_question_extraction_uses_flash_lite_only(monkeypatch, compatible):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs['json']))
        body = {'questions': [{'question': 'Решите 2 + 2'}, {'question': 'Найдите x'}]}
        if compatible:
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(body)}}]})
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(body)}]}}]})
    monkeypatch.setattr('app.ai.gemini.httpx.post', post)
    endpoint = 'https://example.test/v1' if compatible else 'https://generativelanguage.googleapis.com/v1beta/models'
    provider = GeminiAIProvider('key', model='gemini-3.8-flash', recognition_model='gemini-3.8-flash', api_base=endpoint)
    assert provider.extract_questions(PageInput(1, b'photo', 'image/jpeg')) == ['Решите 2 + 2', 'Найдите x']
    url, data = calls[0]
    assert data['model'] == 'gemini-3.5-flash-lite' if compatible else '/gemini-3.5-flash-lite:generateContent' in url
    assert len(calls) == 1


def test_import_requires_auth_and_returns_editable_conditions(harness):
    assert harness.client.post('/v1/question-import', files=[('file', photograph()[1])]).status_code == 401
    auth, _ = harness.register()
    response = harness.client.post('/v1/question-import', headers=auth, files=[('file', photograph()[1])])
    assert response.status_code == 200, response.text
    assert response.json() == {'questions': [{'question': 'Демонстрационное задание: вычислите 2 + 2.'}], 'demonstration': True}


def test_import_rejects_invalid_file_before_ai(build_harness):
    from app.ai import FakeAIProvider
    class Provider(FakeAIProvider):
        def extract_questions(self, page):
            raise AssertionError('invalid file must not be sent to AI')
    harness = build_harness(ai_provider=Provider())
    auth, _ = harness.register()
    response = harness.client.post('/v1/question-import', headers=auth, files={'file': ('bad.jpg', b'not-an-image', 'image/jpeg')})
    assert response.status_code == 415


def test_import_timeout_does_not_resend(build_harness):
    from app.ai import FakeAIProvider
    class Provider(FakeAIProvider):
        calls = 0
        def extract_questions(self, page):
            self.calls += 1
            raise AIError('ai_unavailable', retryable=True)
    provider = Provider()
    harness = build_harness(ai_provider=provider)
    auth, _ = harness.register()
    response = harness.client.post('/v1/question-import', headers=auth, files=[('file', photograph()[1])])
    assert response.status_code == 503
    assert provider.calls == 1
    assert 'платный запрос' in response.json()['error']['message']


@pytest.mark.parametrize('body', [
    {'questions': ['Укажите число электронов', 'Найдите изотопы']},
    ['Укажите число электронов', 'Найдите изотопы'],
    {'questions': [{'question': 'Укажите число электронов'}, 'Найдите изотопы']},
])
def test_question_extraction_accepts_list_of_strings(monkeypatch, body):
    calls = []
    def post(url, **kwargs):
        calls.append(1)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(body, ensure_ascii=False)}}]})
    monkeypatch.setattr('app.ai.gemini.httpx.post', post)
    provider = GeminiAIProvider('key', api_base='https://example.test/v1')
    assert provider.extract_questions(PageInput(1, b'photo', 'image/jpeg')) == [
        'Укажите число электронов', 'Найдите изотопы']
    assert len(calls) == 1

from app.ai import FakeAIProvider, PreliminaryReview, RecognizedTask
from app.telegram_bot import deliver_pending, dispatch, register
from tests.support import photograph


def class_code(harness, auth, reference, *, name='7 А', submission_ids=None):
    response = harness.client.post(f'/v1/telegram/classes/{reference}/code', headers=auth,
                                   json={'name': name, 'submission_ids': submission_ids or []})
    assert response.status_code == 200, response.text
    return response.json()['code']


def test_registration_is_class_scoped_and_does_not_replace_someone_else(harness):
    auth, _ = harness.register()
    first = class_code(harness, auth, 'class1')
    second = class_code(harness, auth, 'class2', name='7 Б')
    factory = harness.app.state.session_factory
    assert 'завершена' in register(factory, first, 'Иванов Иван', 101)
    assert 'уже зарегистрировано' in register(factory, first, 'Иван Иванов', 202)
    assert 'завершена' in register(factory, second, 'Иванов Иван', 202)
    assert 'не найден' in register(factory, 'X' * 16, 'Иванов Иван', 303)
    assert 'укажите' in register(factory, first, 'Иванов', 303)


def test_class_code_is_teacher_owned_and_rotation_preserves_binding(harness):
    auth, _ = harness.register()
    other, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    assert 'завершена' in register(harness.app.state.session_factory, code, 'Иванов Иван', 101)
    new_code = class_code(harness, auth, 'class1')
    assert new_code != code
    assert 'не найден' in register(harness.app.state.session_factory, code, 'Петров Пётр', 202)
    assert 'уже зарегистрировано' in register(harness.app.state.session_factory, new_code, 'Иванов Иван', 202)
    # Deleting a different teacher's matching client-side id has no effect.
    assert harness.client.delete('/v1/telegram/classes/class1', headers=other).status_code == 204
    assert 'завершена' in register(harness.app.state.session_factory, new_code, 'Петров Пётр', 202)


def test_group_chat_is_ignored(harness, monkeypatch):
    calls = []
    monkeypatch.setattr('app.telegram_bot.call_telegram', lambda *args, **kwargs: calls.append(args))
    dispatch('test', {'chat': {'id': -123, 'type': 'group'}, 'from': {'id': 1}, 'text': '/status'}, harness.app.state.session_factory)
    assert calls == []


def test_polling_timeout_is_sent_separately_from_http_timeout(monkeypatch):
    import httpx
    from app.telegram_bot import call_telegram
    captured = {}
    def post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(200, json={'ok': True, 'result': []})
    monkeypatch.setattr('app.telegram_bot.httpx.post', post)
    assert call_telegram('test', 'getUpdates', http_timeout=15, timeout=5)['ok']
    assert captured['json']['timeout'] == 5
    assert captured['timeout'].read == 15


def test_bot_starts_without_gemini_credentials(monkeypatch):
    import app.telegram_bot as bot
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fixture-token')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    monkeypatch.setenv('AI_PROVIDER', 'gemini')
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    captured = []
    monkeypatch.setattr(bot, 'run_polling', lambda token, factory: captured.append(token))
    bot.main()
    assert captured == ['fixture-token']


def test_existing_work_can_be_attached_only_by_its_owner(build_harness, monkeypatch):
    harness = build_harness(sheet_billing_enabled=True)
    owner, _ = harness.register()
    other, _ = harness.register()
    created = harness.client.post('/v1/submissions', headers=owner, files=[photograph()], data={'student_reference': 'Иван Иванов'})
    assert created.status_code == 202
    submission = created.json()
    harness.process(submission['job_id'])
    # A different teacher cannot claim or relabel the work by knowing its identifier.
    other_code = class_code(harness, other, 'sameid', submission_ids=[submission['id']])
    assert 'завершена' in register(harness.app.state.session_factory, other_code, 'Иванов Иван', 202)
    sent = []
    monkeypatch.setattr('app.telegram_bot.call_telegram', lambda *args, **kwargs: sent.append(kwargs) or {'ok': True})
    deliver_pending('test', harness.app.state.session_factory)
    assert sent == []
    own_code = class_code(harness, owner, 'sameid', submission_ids=[submission['id']])
    assert 'завершена' in register(harness.app.state.session_factory, own_code, 'Иванов Иван', 101)
    deliver_pending('test', harness.app.state.session_factory)
    assert sent and all(item['chat_id'] == 101 for item in sent)


def test_checked_work_and_teacher_edit_delivered_only_to_matching_student(build_harness, monkeypatch):
    class Provider(FakeAIProvider):
        def recognize(self, pages, clarification):
            return [RecognizedTask(page_number=1, number='1', answer='камар', answer_state='present')]
        def review_reference(self, task, reference, clarification):
            return PreliminaryReview(status='incorrect', explanation='«камар» вместо «комар».')
    harness = build_harness(ai_provider=Provider(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    factory = harness.app.state.session_factory
    assert 'завершена' in register(factory, code, 'Иванов Иван', 101)
    assert 'завершена' in register(factory, code, 'Петров Пётр', 202)
    import json
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'class_reference': 'class1', 'student_reference': 'Иван Иванов',
                                         'test_questions': json.dumps([{'question': 'Напишите слово', 'answer': 'комар'}])})
    assert response.status_code == 202, response.text
    submission = response.json()
    harness.process(submission['job_id'])
    status_path = f"/v1/telegram/submissions/{submission['id']}/delivery"
    assert harness.client.get(status_path, headers=auth).json()['status'] == 'pending'
    sent = []
    monkeypatch.setattr('app.telegram_bot.call_telegram', lambda token, method, **kwargs: sent.append(kwargs) or {'ok': True})
    deliver_pending('test', factory)
    assert harness.client.get(status_path, headers=auth).json()['status'] == 'sent'
    assert sent and all(item['chat_id'] == 101 for item in sent)
    text = '\n'.join(item['text'] for item in sent)
    assert '<b>Ваш ответ</b>\nкамар' in text
    assert '<b>Ответ учителя</b>\nкомар' in text
    assert '<blockquote>камар</blockquote>' in text
    old_count = len(sent)
    deliver_pending('test', factory)
    assert len(sent) == old_count
    task_id = harness.results(auth, submission['id'])['tasks'][0]['id']
    result = harness.client.patch(f"/v1/submissions/{submission['id']}/tasks/{task_id}", headers=auth,
                                  json={'answer': 'комар', 'explanation': 'Исправлено'})
    assert result.status_code == 200
    assert harness.client.get(status_path, headers=auth).json()['status'] == 'pending'
    deliver_pending('test', factory)
    text = '\n'.join(item['text'] for item in sent[old_count:])
    assert '<b>Ваш ответ</b>\nкомар' in text
    assert '<b>Первоначальное распознавание</b>\nкамар' in text
    assert '<b>Ответ учителя</b>\nкомар' in text
    assert 'Исправлено' in text


def test_failed_multi_message_delivery_resumes_after_last_success(build_harness, monkeypatch):
    from app.telegram_bot import TelegramError
    from app.telegram_models import TelegramDelivery
    from app.core import utcnow
    from sqlalchemy import select
    class Provider(FakeAIProvider):
        def recognize(self, pages, clarification):
            return [RecognizedTask(page_number=1, number='1', answer='длинный ответ ' * 900, answer_state='present')]
    harness = build_harness(ai_provider=Provider(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    factory = harness.app.state.session_factory
    register(factory, code, 'Иванов Иван', 101)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'class_reference': 'class1', 'student_reference': 'Иван Иванов'})
    harness.process(response.json()['job_id'])
    sent = []
    monkeypatch.setattr('app.telegram_bot.sleep', lambda _: None)
    failure = [True]
    def send(token, method, **kwargs):
        if len(sent) == 1 and failure[0]:
            failure[0] = False
            raise TelegramError('temporary')
        sent.append(kwargs['text'])
        return {'ok': True}
    monkeypatch.setattr('app.telegram_bot.call_telegram', send)
    deliver_pending('test', factory)
    assert len(sent) == 1
    first_message = sent[0]
    with factory.begin() as db:
        delivery = db.scalar(select(TelegramDelivery))
        assert delivery.sent_parts == 1
        delivery.next_attempt_at = utcnow()
    deliver_pending('test', factory)
    assert len(sent) > 1
    assert sent.count(first_message) == 1
    with factory() as db:
        assert db.scalar(select(TelegramDelivery)).status == 'sent'


def test_teacher_can_unlink_student_and_prevent_other_teacher_unlink(harness):
    auth, _ = harness.register()
    other, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    register(harness.app.state.session_factory, code, 'Иванов Иван', 101)
    status = harness.client.get('/v1/telegram/classes/class1', headers=auth).json()
    student_id = status['students'][0]['id']
    path = f'/v1/telegram/classes/class1/students/{student_id}'
    assert harness.client.delete(path, headers=other).status_code == 404
    assert harness.client.delete(path, headers=auth).status_code == 204
    assert harness.client.get('/v1/telegram/classes/class1', headers=auth).json()['students'] == []


def test_delivery_retry_is_owner_scoped_and_does_not_recheck_ai(build_harness):
    from app.telegram_models import TelegramDelivery
    from sqlalchemy import select
    harness = build_harness(sheet_billing_enabled=True)
    auth, _ = harness.register()
    other, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    register(harness.app.state.session_factory, code, 'Иванов Иван', 101)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'class_reference': 'class1', 'student_reference': 'Иван Иванов'})
    submission = response.json()
    harness.process(submission['job_id'])
    with harness.app.state.session_factory.begin() as db:
        delivery = db.scalar(select(TelegramDelivery))
        delivery.status, delivery.attempts = 'failed', 5
    path = f"/v1/telegram/submissions/{submission['id']}/retry-delivery"
    assert harness.client.post(path, headers=other).status_code == 404
    assert harness.client.post(path, headers=auth).status_code == 200
    assert harness.client.get(f"/v1/telegram/submissions/{submission['id']}/delivery", headers=auth).json()['status'] == 'pending'
    assert harness.submission(auth, submission['id'])['job_id'] == submission['job_id']

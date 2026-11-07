from app.ai import FakeAIProvider, PreliminaryReview, RecognizedTask
from app.telegram_bot import deliver_pending, dispatch, dispatch_callback, register
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
    assert harness.client.post(f"/v1/submissions/{submission['id']}/confirm", headers=owner).status_code == 200
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
    assert harness.client.get(status_path, headers=auth).json()['status'] == 'awaiting_confirmation'
    sent = []
    monkeypatch.setattr('app.telegram_bot.call_telegram', lambda token, method, **kwargs: sent.append(kwargs) or {'ok': True})
    deliver_pending('test', factory)
    assert sent == []
    assert harness.client.post(f"/v1/submissions/{submission['id']}/confirm", headers=auth).status_code == 200
    deliver_pending('test', factory)
    assert harness.client.get(status_path, headers=auth).json()['status'] == 'sent'
    assert sent and all(item['chat_id'] == 101 for item in sent)
    dispatch_callback('test', {'id': 'cb', 'from': {'id': 101}, 'message': {'chat': {'id': 101, 'type': 'private'}},
                               'data': f"work:{submission['id']}:0"}, factory)
    text = '\n'.join(item.get('text', '') for item in sent)
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
    assert harness.client.get(status_path, headers=auth).json()['status'] == 'awaiting_confirmation'
    deliver_pending('test', factory)
    assert len(sent) == old_count
    assert harness.client.post(f"/v1/submissions/{submission['id']}/confirm", headers=auth).status_code == 200
    deliver_pending('test', factory)
    dispatch_callback('test', {'id': 'cb', 'from': {'id': 101}, 'message': {'chat': {'id': 101, 'type': 'private'}},
                               'data': f"work:{submission['id']}:0"}, factory)
    text = '\n'.join(item.get('text', '') for item in sent[old_count:])
    assert '<b>Ваш ответ</b>\nкомар' in text
    assert '<b>Первоначальное распознавание</b>\nкамар' in text
    assert '<b>Ответ учителя</b>\nкомар' in text
    assert 'Исправлено' in text


def test_failed_notification_is_retried(build_harness, monkeypatch):
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
    assert harness.client.post(f"/v1/submissions/{response.json()['id']}/confirm", headers=auth).status_code == 200
    sent = []
    monkeypatch.setattr('app.telegram_bot.sleep', lambda _: None)
    failure = [True]
    def send(token, method, **kwargs):
        if failure[0]:
            failure[0] = False
            raise TelegramError('temporary')
        sent.append(kwargs['text'])
        return {'ok': True}
    monkeypatch.setattr('app.telegram_bot.call_telegram', send)
    deliver_pending('test', factory)
    assert len(sent) == 0
    with factory.begin() as db:
        delivery = db.scalar(select(TelegramDelivery))
        assert delivery.sent_parts == 0
        delivery.next_attempt_at = utcnow()
    deliver_pending('test', factory)
    assert len(sent) == 1
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
    assert harness.client.post(f"/v1/submissions/{submission['id']}/confirm", headers=auth).status_code == 200
    with harness.app.state.session_factory.begin() as db:
        delivery = db.scalar(select(TelegramDelivery))
        delivery.status, delivery.attempts = 'failed', 5
    path = f"/v1/telegram/submissions/{submission['id']}/retry-delivery"
    assert harness.client.post(path, headers=other).status_code == 404
    assert harness.client.post(path, headers=auth).status_code == 200
    assert harness.client.get(f"/v1/telegram/submissions/{submission['id']}/delivery", headers=auth).json()['status'] == 'pending'
    assert harness.submission(auth, submission['id'])['job_id'] == submission['job_id']


def test_profile_requires_all_tasks_confirmed_and_checks_callback_owner(build_harness, monkeypatch):
    class Provider(FakeAIProvider):
        def recognize(self, pages, clarification):
            return [RecognizedTask(page_number=1, number=str(i), answer='2', answer_state='present') for i in (1, 2)]
    harness = build_harness(ai_provider=Provider(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    factory = harness.app.state.session_factory
    register(factory, code, 'Иванов Иван', 101)
    register(factory, code, 'Петров Пётр', 202)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'class_reference': 'class1', 'student_reference': 'Иван Иванов'})
    submission = response.json()
    harness.process(submission['job_id'])
    tasks = harness.results(auth, submission['id'])['tasks']
    assert harness.client.post(f"/v1/submissions/{submission['id']}/tasks/{tasks[0]['id']}/confirm", headers=auth).status_code == 200
    calls = []
    monkeypatch.setattr('app.telegram_bot.call_telegram', lambda token, method, **kw: calls.append((method, kw)) or {'ok': True})
    dispatch('test', {'chat': {'id': 101, 'type': 'private'}, 'from': {'id': 101}, 'text': '/start'}, factory)
    assert 'пока нет' in calls[-1][1]['text']
    deliver_pending('test', factory)
    assert len(calls) == 1
    assert harness.client.post(f"/v1/submissions/{submission['id']}/tasks/{tasks[1]['id']}/confirm", headers=auth).status_code == 200
    dispatch('test', {'chat': {'id': 101, 'type': 'private'}, 'from': {'id': 101}, 'text': '/start'}, factory)
    assert calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data'].startswith('work:')
    calls.clear()
    dispatch_callback('test', {'id': 'cb', 'from': {'id': 202}, 'message': {'chat': {'id': 202, 'type': 'private'}},
                               'data': f"work:{submission['id']}:0"}, factory)
    assert any('недоступна' in item.get('text', '') for _, item in calls)
    assert not any('Ваш ответ' in item.get('text', '') for _, item in calls)


def test_profile_pagination_lists_only_five_works(build_harness, monkeypatch):
    harness = build_harness(sheet_billing_enabled=True)
    auth, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    factory = harness.app.state.session_factory
    register(factory, code, 'Иванов Иван', 101)
    for _ in range(6):
        response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                       data={'class_reference': 'class1', 'student_reference': 'Иван Иванов'})
        submission = response.json()
        harness.process(submission['job_id'])
        assert harness.client.post(f"/v1/submissions/{submission['id']}/confirm", headers=auth).status_code == 200
    calls = []
    monkeypatch.setattr('app.telegram_bot.call_telegram', lambda token, method, **kw: calls.append((method, kw)) or {'ok': True})
    dispatch('test', {'chat': {'id': 101, 'type': 'private'}, 'from': {'id': 101}, 'text': '/start'}, factory)
    keyboard = calls[-1][1]['reply_markup']['inline_keyboard']
    assert sum(button['callback_data'].startswith('work:') for row in keyboard for button in row) == 5
    assert any(button['callback_data'] == 'page:1' for row in keyboard for button in row)
    dispatch_callback('test', {'id': 'cb', 'from': {'id': 101},
        'message': {'message_id': 42, 'chat': {'id': 101, 'type': 'private'}}, 'data': 'page:1'}, factory)
    assert calls[-1][0] == 'editMessageText'
    keyboard = calls[-1][1]['reply_markup']['inline_keyboard']
    assert sum(button['callback_data'].startswith('work:') for row in keyboard for button in row) == 1
    assert any(button['callback_data'] == 'page:0' for row in keyboard for button in row)


def test_teacher_sends_message_and_files_only_after_confirmation(build_harness, monkeypatch):
    import httpx
    harness = build_harness(sheet_billing_enabled=True, telegram_bot_token='test-token')
    auth, _ = harness.register()
    other, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    register(harness.app.state.session_factory, code, 'Иванов Иван', 101)
    response = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
                                   data={'class_reference': 'class1', 'student_reference': 'Иван Иванов'})
    submission = response.json()
    harness.process(submission['job_id'])
    path = f"/v1/telegram/submissions/{submission['id']}/message"
    calls = []
    def post(url, **kwargs):
        calls.append((url.rsplit('/', 1)[-1], kwargs))
        return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr('app.telegram_access.httpx.post', post)
    assert harness.client.post(path, headers=other, data={'text': 'Чужой доступ'}).status_code == 404
    assert harness.client.post(path, headers=auth, data={'text': 'До подтверждения'}).status_code == 409
    assert calls == []
    assert harness.client.post(f"/v1/submissions/{submission['id']}/confirm", headers=auth).status_code == 200
    result = harness.client.post(path, headers=auth, data={'text': 'Посмотрите подсказку'},
                                 files=[('files', ('help.pdf', b'%PDF-1.4 help', 'application/pdf'))])
    assert result.status_code == 200, result.text
    assert result.json()['sent_parts'] == 2
    assert [method for method, _ in calls] == ['sendMessage', 'sendDocument']
    assert all(data['data']['chat_id'] == 101 for _, data in calls)
    assert calls[1][1]['files']['document'][1] == b'%PDF-1.4 help'
    assert harness.submission(auth, submission['id'])['job_id'] == submission['job_id']


def test_teacher_message_reports_partial_telegram_failure(build_harness, monkeypatch):
    import httpx
    harness = build_harness(sheet_billing_enabled=True, telegram_bot_token='test-token')
    auth, _ = harness.register()
    code = class_code(harness, auth, 'class1')
    register(harness.app.state.session_factory, code, 'Иванов Иван', 101)
    submission = harness.client.post('/v1/submissions', headers=auth, files=[photograph()],
        data={'class_reference': 'class1', 'student_reference': 'Иван Иванов'}).json()
    harness.process(submission['job_id'])
    assert harness.client.post(f"/v1/submissions/{submission['id']}/confirm", headers=auth).status_code == 200
    def post(url, **kwargs):
        return httpx.Response(200 if url.endswith('sendMessage') else 500,
                              json={'ok': url.endswith('sendMessage')})
    monkeypatch.setattr('app.telegram_access.httpx.post', post)
    result = harness.client.post(f"/v1/telegram/submissions/{submission['id']}/message", headers=auth,
        data={'text': 'Проверьте'}, files=[('files', ('help.txt', b'help', 'text/plain'))])
    assert result.status_code == 502
    assert 'частей: 1' in result.text

"""Student Telegram bot. Run one instance: python -m app.telegram_bot."""
import hashlib
import html
import json
import logging
import signal
from datetime import timedelta
from threading import Event
from time import sleep

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.core import configure_logging, event, utcnow
from app.db import make_session_factory
from app.submission_models import DetectedTask, Submission, TaskAnalysis
from app.telegram_access import fully_confirmed, name_key, schedule_submission, valid_name
from app.telegram_models import TelegramBinding, TelegramClass, TelegramDelivery
from app.telegram_notifier import format_result
from app.workspace import TeacherWorkspace

logging.getLogger('httpx').setLevel(logging.WARNING)
API = 'https://api.telegram.org/bot{token}/{method}'
HELP = ('Здравствуйте! Чтобы получать результаты, напишите в личном чате:\n'
        '/link КОД_КЛАССА Фамилия Имя\n\n'
        'Код выдаёт учитель. После регистрации результаты будут приходить автоматически.\n'
        '/status — проверить регистрацию\n/unlink — отключить все привязки')
PAGE_SIZE = 5


def _work_title(db, row):
    workspace = db.get(TeacherWorkspace, row.user_id)
    data = workspace.data if workspace and isinstance(workspace.data, dict) else {}
    link = (data.get('links') or {}).get(row.id) or {}
    test_id = link.get('testId') if isinstance(link, dict) else None
    return next((test.get('name') for test in data.get('tests', [])
                 if isinstance(test, dict) and test.get('id') == test_id), None) or 'Работа'


def _student_works(db, chat_id):
    bindings = db.scalars(select(TelegramBinding).where(TelegramBinding.chat_id == chat_id)).all()
    works = []
    for binding in bindings:
        cls = db.get(TelegramClass, binding.class_id)
        if not cls:
            continue
        rows = db.scalars(select(Submission).where(Submission.user_id == cls.user_id,
                          Submission.class_reference == cls.external_id,
                          Submission.delete_requested_at.is_(None), Submission.first_completed_at.is_not(None))
                          .order_by(Submission.created_at.desc())).all()
        for row in rows:
            if name_key(row.student_reference) == binding.name_key and fully_confirmed(db, row):
                works.append((row, cls, binding, _work_title(db, row)))
    works.sort(key=lambda item: (item[0].created_at, item[0].id), reverse=True)
    return bindings, works


def _task_data(db, tasks):
    latest_inputs = {}
    if tasks:
        analyses = db.scalars(select(TaskAnalysis).where(TaskAnalysis.task_id.in_([task.id for task in tasks]))
                              .order_by(TaskAnalysis.created_at.desc(), TaskAnalysis.id.desc())).all()
        for analysis in analyses:
            latest_inputs.setdefault(analysis.task_id, analysis.review_input)
    result = []
    for task in tasks:
        teacher = task.teacher or {}
        prior = latest_inputs.get(task.id)
        stale = any(field in teacher and (prior is None or (teacher[field] or None) != (prior.get(field) or None))
                    for field in ('number', 'condition', 'answer'))
        result.append({'original_recognition': task.original_recognition, 'recognition': task.recognition,
                       'preliminary': task.preliminary, 'teacher': task.teacher, 'review_stale': stale})
    return result


def show_profile(token, chat_id, db_factory, page=0, message_id=None):
    with db_factory() as db:
        bindings, works = _student_works(db, chat_id)
        if not bindings:
            reply(token, chat_id, HELP)
            return
        page = min(max(0, page), max(0, (len(works) - 1) // PAGE_SIZE))
        classes = [db.get(TelegramClass, binding.class_id) for binding in bindings]
        names = ', '.join(dict.fromkeys(binding.name for binding in bindings))
        class_names = ', '.join(dict.fromkeys(cls.name for cls in classes if cls))
        title = f'<b>Профиль ученика</b>\n{html.escape(names)}\nКласс: {html.escape(class_names)}'
        title += f'\n\n<b>Мои работы · {len(works)}</b>' if works else '\n\nПодтверждённых работ пока нет.'
        keyboard = []
        for row, cls, binding, name in works[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            date = row.created_at.strftime('%d.%m.%Y')
            label = f'{name[:32]} · {cls.name[:12]} · {date}'
            keyboard.append([{'text': label, 'callback_data': f'work:{row.id}:{page}'}])
        pages = max(1, (len(works) + PAGE_SIZE - 1) // PAGE_SIZE)
        nav = []
        if page > 0:
            nav.append({'text': '← Новее', 'callback_data': f'page:{page-1}'})
        if pages > 1:
            nav.append({'text': f'{page+1} / {pages}', 'callback_data': 'noop'})
        if page + 1 < pages:
            nav.append({'text': 'Старее →', 'callback_data': f'page:{page+1}'})
        if nav:
            keyboard.append(nav)
    payload = {'chat_id': chat_id, 'text': title, 'parse_mode': 'HTML',
               'reply_markup': {'inline_keyboard': keyboard}}
    if message_id is not None:
        payload['message_id'] = message_id
    call_telegram(token, 'editMessageText' if message_id is not None else 'sendMessage', **payload)


def dispatch_callback(token, callback, db_factory):
    sender = callback.get('from') or {}
    message = callback.get('message') or {}
    chat = message.get('chat') or {}
    callback_id = callback.get('id')
    if chat.get('type') != 'private' or not isinstance(chat.get('id'), int) or chat['id'] != sender.get('id'):
        return
    chat_id = chat['id']
    if callback_id:
        call_telegram(token, 'answerCallbackQuery', callback_query_id=callback_id)
    data = callback.get('data') or ''
    if data.startswith('page:') and data[5:].isdigit():
        show_profile(token, chat_id, db_factory, int(data[5:]), message.get('message_id'))
    elif data.startswith('work:'):
        parts = data.split(':')
        if len(parts) != 3 or not parts[2].isdigit():
            return
        with db_factory() as db:
            _, works = _student_works(db, chat_id)
            match = next((entry for entry in works if entry[0].id == parts[1]), None)
            if match is None:
                reply(token, chat_id, 'Работа недоступна. Обновите профиль командой /start.')
                return
            row, cls, binding, name = match
            tasks = db.scalars(select(DetectedTask).where(DetectedTask.submission_id == row.id)
                               .order_by(DetectedTask.position)).all()
            details = _task_data(db, tasks)
            submission = {'created_at': row.created_at.isoformat(), 'title': name, 'class_name': cls.name}
            messages = format_result(submission, details)
        for index, text in enumerate(messages):
            keyboard = {'inline_keyboard': [[{'text': '← Мои работы', 'callback_data': f'page:{parts[2]}'}]]} if index == len(messages)-1 else None
            payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
            if keyboard:
                payload['reply_markup'] = keyboard
            call_telegram(token, 'sendMessage', **payload)


class TelegramError(Exception):
    pass


def call_telegram(token: str, method: str, *, http_timeout: float = 20, **payload) -> dict:
    try:
        response = httpx.post(API.format(token=token, method=method), json=payload,
                              timeout=httpx.Timeout(http_timeout, connect=5))
        if not response.is_success:
            raise TelegramError(f'telegram_http_{response.status_code}')
        data = response.json()
        if not isinstance(data, dict) or data.get('ok') is not True:
            raise TelegramError('telegram_api_error')
        return data
    except (httpx.RequestError, ValueError) as error:
        raise TelegramError('telegram_unavailable') from error


def reply(token: str, chat_id: int, text: str) -> None:
    call_telegram(token, 'sendMessage', chat_id=chat_id, text=text)


def register(db_factory, code: str, name: str, chat_id: int) -> str:
    name = ' '.join(name.split())
    if not valid_name(name) or not code.isascii() or len(code) != 16:
        return 'Проверьте код класса и укажите фамилию и имя: /link КОД_КЛАССА Фамилия Имя'
    digest = hashlib.sha256(code.encode('ascii')).hexdigest()
    try:
        with db_factory.begin() as db:
            cls = db.scalar(select(TelegramClass).where(TelegramClass.code_digest == digest).with_for_update())
            if cls is None:
                return 'Код класса не найден. Попросите учителя проверить его.'
            key = name_key(name)
            by_name = db.scalar(select(TelegramBinding).where(TelegramBinding.class_id == cls.id,
                                                               TelegramBinding.name_key == key))
            by_chat = db.scalar(select(TelegramBinding).where(TelegramBinding.class_id == cls.id,
                                                               TelegramBinding.chat_id == chat_id))
            if by_name and by_name.chat_id != chat_id:
                return 'Это имя уже зарегистрировано в классе. Обратитесь к учителю.'
            if by_chat and by_chat.name_key != key:
                return 'Этот чат уже зарегистрирован в классе под другим именем. Сначала используйте /unlink.'
            binding = by_name or by_chat
            if binding is None:
                binding = TelegramBinding(class_id=cls.id, name=name, name_key=key, chat_id=chat_id)
                db.add(binding)
                db.flush()
            # Link already processed work of this student, without trusting a class name alone.
            rows = db.scalars(select(Submission).where(Submission.user_id == cls.user_id,
                              Submission.class_reference == cls.external_id,
                              Submission.first_completed_at.is_not(None), Submission.delete_requested_at.is_(None))).all()
            for row in rows:
                if name_key(row.student_reference) == key:
                    # Serialize with worker changes to avoid duplicate delivery rows.
                    locked = db.scalar(select(Submission).where(Submission.id == row.id).with_for_update())
                    schedule_submission(db, locked)
            return f'Регистрация в классе «{cls.name}» завершена. Результаты будут приходить сюда.'
    except IntegrityError:
        return 'Регистрация уже выполняется. Попробуйте ещё раз.'


def dispatch(token: str, message: dict, db_factory) -> None:
    chat = message.get('chat') or {}
    sender = message.get('from') or {}
    if chat.get('type') != 'private' or not isinstance(chat.get('id'), int) or chat['id'] != sender.get('id'):
        return
    chat_id = chat['id']
    text = (message.get('text') or '').strip()
    if not text:
        return
    command = text.split(None, 1)[0].split('@')[0].lower()
    if command == '/start':
        show_profile(token, chat_id, db_factory)
    elif command == '/help':
        reply(token, chat_id, HELP)
    elif command == '/link':
        parts = text.split(None, 2)
        result = register(db_factory, parts[1], parts[2], chat_id) if len(parts) == 3 else HELP
        reply(token, chat_id, result)
        if 'завершена' in result:
            show_profile(token, chat_id, db_factory)
    elif command == '/status':
        with db_factory() as db:
            bindings = db.execute(select(TelegramClass.name, TelegramBinding.name).join(
                TelegramBinding, TelegramBinding.class_id == TelegramClass.id).where(TelegramBinding.chat_id == chat_id)).all()
        text = ('Регистрация:\n' + '\n'.join(f'• {cls}: {name}' for cls, name in bindings)) if bindings else 'Регистрации нет. ' + HELP
        reply(token, chat_id, text)
    elif command == '/unlink':
        with db_factory.begin() as db:
            for binding in db.scalars(select(TelegramBinding).where(TelegramBinding.chat_id == chat_id)).all():
                db.delete(binding)
        reply(token, chat_id, 'Все ваши регистрации отключены. Для новой регистрации используйте /link.')
    else:
        reply(token, chat_id, 'Команды: /start, /link, /status, /unlink.')


def _claim_delivery(db_factory):
    with db_factory.begin() as db:
        item = db.scalar(select(TelegramDelivery).where(
            TelegramDelivery.status == 'pending', TelegramDelivery.next_attempt_at <= utcnow()
        ).order_by(TelegramDelivery.next_attempt_at, TelegramDelivery.id).limit(1).with_for_update(skip_locked=True))
        if item is None:
            return None
        row = db.get(Submission, item.submission_id)
        binding = db.get(TelegramBinding, item.binding_id)
        cls = db.get(TelegramClass, binding.class_id) if binding else None
        if (not row or not binding or not cls or row.delete_requested_at is not None
                or row.user_id != cls.user_id or row.class_reference != cls.external_id
                or name_key(row.student_reference) != binding.name_key):
            item.status = 'failed'
            return None
        if row.status in {'queued', 'processing'}:
            item.next_attempt_at = utcnow() + timedelta(seconds=15)
            return None
        if row.first_completed_at is None:
            item.status = 'failed'
            return None
        if not fully_confirmed(db, row):
            db.delete(item)
            return None
        tasks = db.scalars(select(DetectedTask).where(DetectedTask.submission_id == row.id)
                           .order_by(DetectedTask.position)).all()
        data = _task_data(db, tasks)
        submission = {'id': row.id, 'title': _work_title(db, row), 'created_at': row.created_at.isoformat()}
        digest = hashlib.sha256(json.dumps([submission, data], ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
        if item.inflight_digest != digest:
            item.inflight_digest, item.sent_parts = digest, 0
        item.attempts += 1
        claim_attempt = item.attempts
        item.next_attempt_at = utcnow() + timedelta(minutes=5)
        return item.id, claim_attempt, binding.id, binding.chat_id, digest, submission, data, item.digest, item.sent_parts


def deliver_pending(token: str, db_factory, *, limit: int = 20) -> None:
    for _ in range(limit):
        claimed = _claim_delivery(db_factory)
        if claimed is None:
            return
        item_id, attempt, binding_id, chat_id, digest, submission, tasks, previous_digest, sent_parts = claimed
        success = False
        try:
            if digest != previous_digest:
                notice = (f'<b>Работа проверена</b>\n{html.escape(submission["title"])}\n\n'
                          'Учитель подтвердил результат. Откройте разбор ниже.')
                for index, message in enumerate([notice]):
                    if index < sent_parts:
                        continue
                    if index > sent_parts:
                        sleep(1.05)
                    with db_factory() as db:
                        current_row = db.get(Submission, submission['id'])
                        if (db.get(TelegramBinding, binding_id) is None or db.get(TelegramDelivery, item_id) is None
                                or current_row is None or not fully_confirmed(db, current_row)):
                            return
                    call_telegram(token, 'sendMessage', chat_id=chat_id, text=message, parse_mode='HTML',
                                  reply_markup={'inline_keyboard': [[{'text': 'Открыть разбор',
                                      'callback_data': f'work:{submission["id"]}:0'}]]})
                    with db_factory.begin() as db:
                        current = db.scalar(select(TelegramDelivery).where(TelegramDelivery.id == item_id).with_for_update())
                        if current is None or current.status != 'pending' or current.attempts != attempt or current.inflight_digest != digest:
                            break
                        current.sent_parts = index + 1
                else:
                    success = True
            else:
                success = True
        except TelegramError:
            event('telegram_delivery_failed', component='telegram', level='WARNING', error_code='telegram_unavailable')
        with db_factory.begin() as db:
            item = db.scalar(select(TelegramDelivery).where(TelegramDelivery.id == item_id).with_for_update())
            if item is None or item.status != 'pending' or item.attempts != attempt:
                continue
            if success:
                item.status, item.digest = 'sent', digest
            elif attempt >= 5:
                item.status = 'failed'
            else:
                item.next_attempt_at = utcnow() + timedelta(seconds=min(30 * 2 ** (attempt - 1), 600))


def run_polling(token: str, db_factory) -> None:
    stopped = Event()
    def stop(_signum, _frame):
        stopped.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    offset = None
    connected = False
    event('telegram_bot_started', component='telegram')
    while not stopped.is_set():
        try:
            deliver_pending(token, db_factory)
            payload = {'timeout': 5, 'allowed_updates': ['message', 'callback_query']}
            if offset is not None:
                payload['offset'] = offset
            updates = call_telegram(token, 'getUpdates', http_timeout=15, **payload).get('result', [])
            if not connected:
                event('telegram_poll_ready', component='telegram')
                connected = True
            for update in updates:
                try:
                    if update.get('message'):
                        dispatch(token, update['message'], db_factory)
                    elif update.get('callback_query'):
                        dispatch_callback(token, update['callback_query'], db_factory)
                except Exception:
                    event('telegram_command_failed', component='telegram', level='WARNING', error_code='command_failed')
                    stopped.wait(5)
                    break
                offset = update['update_id'] + 1
        except TelegramError as error:
            event('telegram_poll_failed', component='telegram', level='WARNING', error_code=str(error))
            stopped.wait(5)
        except Exception:
            event('telegram_poll_failed', component='telegram', level='WARNING', error_code='database_or_internal')
            stopped.wait(5)
    event('telegram_bot_stopped', component='telegram')


def main() -> None:
    # The bot only needs its token and the shared database. Do not read Gemini
    # credentials from .env into this process; the service launcher supplies env.
    settings = Settings(_env_file=None, ai_provider='fake', gemini_api_key=None)
    configure_logging(settings.log_level)
    if not settings.telegram_bot_token:
        raise SystemExit('Укажите TELEGRAM_BOT_TOKEN в .env перед запуском бота.')
    run_polling(settings.telegram_bot_token.get_secret_value(), make_session_factory(settings.database_url))


if __name__ == '__main__':
    main()

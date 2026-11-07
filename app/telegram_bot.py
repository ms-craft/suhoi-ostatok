"""Student Telegram bot. Run one instance: python -m app.telegram_bot."""
import hashlib
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
from app.telegram_access import name_key, schedule_submission, valid_name
from app.telegram_models import TelegramBinding, TelegramClass, TelegramDelivery
from app.telegram_notifier import format_result

logging.getLogger('httpx').setLevel(logging.WARNING)
API = 'https://api.telegram.org/bot{token}/{method}'
HELP = ('Здравствуйте! Чтобы получать результаты, напишите в личном чате:\n'
        '/link КОД_КЛАССА Фамилия Имя\n\n'
        'Код выдаёт учитель. После регистрации результаты будут приходить автоматически.\n'
        '/status — проверить регистрацию\n/unlink — отключить все привязки')


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
    if command in {'/start', '/help'}:
        reply(token, chat_id, HELP)
    elif command == '/link':
        parts = text.split(None, 2)
        reply(token, chat_id, register(db_factory, parts[1], parts[2], chat_id) if len(parts) == 3 else HELP)
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
        tasks = db.scalars(select(DetectedTask).where(DetectedTask.submission_id == row.id)
                           .order_by(DetectedTask.position)).all()
        latest_inputs = {}
        if tasks:
            analyses = db.scalars(select(TaskAnalysis).where(TaskAnalysis.task_id.in_([task.id for task in tasks]))
                                  .order_by(TaskAnalysis.created_at.desc(), TaskAnalysis.id.desc())).all()
            for analysis in analyses:
                latest_inputs.setdefault(analysis.task_id, analysis.review_input)
        def review_is_stale(task):
            teacher = task.teacher or {}
            prior = latest_inputs.get(task.id)
            for field in ('condition', 'answer'):
                if field in teacher:
                    if prior is None or (teacher[field] or None) != (prior.get(field) or None):
                        return True
            return False
        data = [{'original_recognition': task.original_recognition, 'recognition': task.recognition,
                 'preliminary': task.preliminary, 'teacher': task.teacher, 'review_stale': review_is_stale(task)}
                for task in tasks]
        submission = {'created_at': row.created_at.isoformat()}
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
                for index, message in enumerate(format_result(submission, tasks)):
                    if index < sent_parts:
                        continue
                    if index > sent_parts:
                        sleep(1.05)
                    with db_factory() as db:
                        if db.get(TelegramBinding, binding_id) is None or db.get(TelegramDelivery, item_id) is None:
                            return
                    call_telegram(token, 'sendMessage', chat_id=chat_id, text=message, parse_mode='HTML')
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
            payload = {'timeout': 5, 'allowed_updates': ['message']}
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

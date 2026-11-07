"""Telegram bot process for student self-registration.

Run as a standalone service:

    python -m app.telegram_bot

The bot handles three commands:

/start
    Greeting + instructions.

/link <имя>
    Bind this Telegram chat to the given student_reference label
    (the same string the teacher sets in the web UI).
    Example: /link Иванов 8А

/unlink
    Remove the binding for this chat.

/status
    Show current binding for this chat.
"""

import logging
import signal
import sys
from threading import Event

from app.config import get_settings
from app.core import configure_logging, new_id, utcnow

logger = logging.getLogger("stopkadr")


# ---------------------------------------------------------------------------
# Telegram Bot API helpers (using httpx — no extra dependency needed)
# ---------------------------------------------------------------------------

import httpx

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, **payload) -> dict:
    url = _API.format(token=token, method=method)
    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _reply(token: str, chat_id: int, text: str) -> None:
    try:
        _call(token, "sendMessage",
              chat_id=chat_id, text=text,
              parse_mode="Markdown",
              disable_web_page_preview=True)
    except Exception as exc:
        logger.warning("tg_reply_failed", extra={"event_fields": {
            "component": "telegram_bot", "chat_id": chat_id, "error": str(exc)[:200],
        }})


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "👋 Привет! Я бот для получения результатов проверки самостоятельных работ.\n\n"
    "Команды:\n"
    "• `/link Фамилия Класс` — привязать этот чат к вашему имени в системе\n"
    "  Пример: `/link Иванов 8А`\n"
    "• `/status` — показать текущую привязку\n"
    "• `/unlink` — отвязать этот чат\n\n"
    "После привязки вы будете автоматически получать разбор каждой проверенной работы."
)


def handle_start(token: str, chat_id: int, _text: str, _db_factory) -> None:
    _reply(token, chat_id, HELP_TEXT)


def handle_status(token: str, chat_id: int, _text: str, db_factory) -> None:
    from sqlalchemy import select
    from app.telegram_models import TelegramBinding

    with db_factory() as db:
        binding = db.scalar(
            select(TelegramBinding).where(TelegramBinding.chat_id == chat_id)
        )
    if binding:
        _reply(token, chat_id,
               f"✅ Ваш чат привязан к имени: *{binding.student_reference}*")
    else:
        _reply(token, chat_id,
               "❌ Привязка не найдена. Используйте `/link Фамилия Класс` для регистрации.")


def handle_link(token: str, chat_id: int, text: str, db_factory) -> None:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.telegram_models import TelegramBinding

    # Extract name: everything after "/link "
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        _reply(token, chat_id,
               "⚠️ Укажите ваше имя после команды.\nПример: `/link Иванов 8А`")
        return

    student_ref = parts[1].strip()[:120]

    with db_factory.begin() as db:
        # Remove any existing binding for this chat
        existing_chat = db.scalar(
            select(TelegramBinding).where(TelegramBinding.chat_id == chat_id)
        )
        if existing_chat:
            db.delete(existing_chat)
            db.flush()

        # Remove any existing binding for this name (another chat had it)
        existing_ref = db.scalar(
            select(TelegramBinding).where(TelegramBinding.student_reference == student_ref)
        )
        if existing_ref:
            db.delete(existing_ref)
            db.flush()

        db.add(TelegramBinding(
            id=new_id(),
            student_reference=student_ref,
            chat_id=chat_id,
        ))

    _reply(token, chat_id,
           f"✅ Готово! Теперь вы будете получать разбор работ как *{student_ref}*.\n\n"
           "Убедитесь, что учитель указывает это имя точно так же при загрузке работы.")


def handle_unlink(token: str, chat_id: int, _text: str, db_factory) -> None:
    from sqlalchemy import select
    from app.telegram_models import TelegramBinding

    with db_factory.begin() as db:
        binding = db.scalar(
            select(TelegramBinding).where(TelegramBinding.chat_id == chat_id)
        )
        if binding:
            name = binding.student_reference
            db.delete(binding)
        else:
            name = None

    if name:
        _reply(token, chat_id, f"✅ Привязка к имени *{name}* удалена.")
    else:
        _reply(token, chat_id, "ℹ️ Привязка не была установлена.")


HANDLERS = {
    "/start": handle_start,
    "/help": handle_start,
    "/link": handle_link,
    "/unlink": handle_unlink,
    "/status": handle_status,
}


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def _dispatch(token: str, message: dict, db_factory) -> None:
    chat_id: int = message["chat"]["id"]
    text: str = (message.get("text") or "").strip()
    if not text:
        return

    # Strip bot username suffix (@BotName) from commands
    command = text.split()[0].split("@")[0].lower()

    handler = HANDLERS.get(command)
    if handler:
        try:
            handler(token, chat_id, text, db_factory)
        except Exception as exc:
            logger.error("tg_handler_error", extra={"event_fields": {
                "component": "telegram_bot",
                "command": command,
                "chat_id": chat_id,
                "error": str(exc)[:300],
            }})
            _reply(token, chat_id, "⚠️ Произошла внутренняя ошибка. Попробуйте позже.")
    else:
        _reply(token, chat_id,
               "Неизвестная команда. Напишите /start для справки.")


def run_polling(token: str, db_factory, poll_timeout: int = 30) -> None:
    offset: int | None = None
    stopped = Event()

    def stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    logger.info("telegram_bot_started", extra={"event_fields": {"component": "telegram_bot"}})

    while not stopped.is_set():
        try:
            params: dict = {"timeout": poll_timeout, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            data = _call(token, "getUpdates", **params)
            updates: list[dict] = data.get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    _dispatch(token, message, db_factory)
        except Exception as exc:
            if not stopped.is_set():
                logger.warning("telegram_poll_error", extra={"event_fields": {
                    "component": "telegram_bot", "error": str(exc)[:300],
                }})
                stopped.wait(5)  # back-off before retry

    logger.info("telegram_bot_stopped", extra={"event_fields": {"component": "telegram_bot"}})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set. Exiting.", file=sys.stderr)
        sys.exit(1)

    from app.main import create_app
    state = create_app(settings).state
    token = settings.telegram_bot_token.get_secret_value()
    run_polling(token, state.session_factory)


if __name__ == "__main__":
    main()

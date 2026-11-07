"""Standalone Telegram bot runner for local development with SQLite.

Usage:
    python run_bot_local.py

Reads TELEGRAM_BOT_TOKEN from .env file automatically.
Stores data in bot_local.db (SQLite) in the project root.

Identification: Фамилия + Имя + Класс  (e.g. "Иванов Алексей 8А")
"""

import logging
import os
import signal
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    print(
        "Ошибка: TELEGRAM_BOT_TOKEN не задан.\n"
        "Добавьте в файл .env строку:\n  TELEGRAM_BOT_TOKEN=ваш_токен",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgbot")

# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "bot_local.db"

try:
    import sqlite3
except ImportError:
    print("sqlite3 не найден (входит в стандартную библиотеку Python).", file=sys.stderr)
    sys.exit(1)

try:
    import httpx
except ImportError:
    print("Не установлен httpx.\nВыполните: pip install httpx", file=sys.stderr)
    sys.exit(1)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS telegram_bindings (
                id                 TEXT PRIMARY KEY,
                student_reference  TEXT NOT NULL UNIQUE,
                last_name          TEXT NOT NULL,
                first_name         TEXT NOT NULL,
                class_name         TEXT NOT NULL,
                lookup_key         TEXT NOT NULL UNIQUE,
                chat_id            INTEGER NOT NULL UNIQUE,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS telegram_notifications (
                id             TEXT PRIMARY KEY,
                submission_id  TEXT,
                chat_id        INTEGER NOT NULL,
                status         TEXT NOT NULL,
                error          TEXT,
                created_at     TEXT NOT NULL
            );
        """)
    log.info("База данных: %s", DB_PATH)


def make_lookup_key(last_name: str, first_name: str, class_name: str) -> str:
    def norm(s: str) -> str:
        return unicodedata.normalize("NFKC", s).strip().casefold()
    return f"{norm(last_name)}:{norm(first_name)}:{norm(class_name)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------
_API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, **payload) -> dict:
    url = _API.format(token=TOKEN, method=method)
    resp = httpx.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


def reply(chat_id: int, text: str, keyboard: dict | None = None) -> None:
    """Отправить текстовое сообщение, опционально с inline-клавиатурой."""
    params: dict = dict(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    if keyboard:
        params["reply_markup"] = keyboard
    try:
        _call("sendMessage", **params)
    except Exception as e:
        log.warning("Не удалось отправить сообщение chat_id=%s: %s", chat_id, e)


def edit_reply(chat_id: int, message_id: int, text: str, keyboard: dict | None = None) -> None:
    """Редактировать уже отправленное сообщение (для callback-ответов)."""
    params: dict = dict(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    if keyboard:
        params["reply_markup"] = keyboard
    try:
        _call("editMessageText", **params)
    except Exception as e:
        log.warning("Не удалось обновить сообщение: %s", e)


def answer_callback(callback_id: str) -> None:
    try:
        _call("answerCallbackQuery", callback_query_id=callback_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------
def main_menu_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✏️ Привязать имя", "callback_data": "action:link"},
                {"text": "📋 Мой статус",    "callback_data": "action:status"},
            ],
            [
                {"text": "❌ Отвязать",       "callback_data": "action:unlink"},
                {"text": "❓ Помощь",          "callback_data": "action:help"},
            ],
        ]
    }


def back_keyboard() -> dict:
    return {
        "inline_keyboard": [[
            {"text": "⬅️ Главное меню", "callback_data": "action:menu"},
        ]]
    }


# ---------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------
WELCOME = (
    "👋 *Добро пожаловать!*\n\n"
    "Я отправляю вам разбор ошибок после проверки самостоятельных работ.\n\n"
    "Выберите действие:"
)

HELP_TEXT = (
    "📖 *Справка*\n\n"
    "Чтобы получать результаты проверки:\n\n"
    "1️⃣ Нажмите *«Привязать имя»*\n"
    "2️⃣ Отправьте: `Фамилия Имя Класс`\n"
    "   Пример: `Иванов Алексей 8А`\n\n"
    "3️⃣ После проверки работы вы получите разбор автоматически\n\n"
    "⚠️ Укажите Фамилию и Имя точно так же, как *вы* подписываете вашу работу."
)

LINK_PROMPT = (
    "✏️ *Привязка имени*\n\n"
    "Отправьте ваши данные в формате:\n"
    "`Фамилия Имя Класс`\n\n"
    "Пример:\n"
    "`Иванов Алексей 8А`\n\n"
    "⚠️ Укажите Фамилию и Имя точно так же, как вы подписываете вашу работу."
)

# Состояния ожидания ввода: chat_id → что ждём
_waiting: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------
def get_binding(chat_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM telegram_bindings WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def show_menu(chat_id: int) -> None:
    reply(chat_id, WELCOME, main_menu_keyboard())


def show_status(chat_id: int, message_id: int | None = None) -> None:
    row = get_binding(chat_id)
    if row:
        text = (
            "📋 *Ваша привязка:*\n\n"
            f"👤 Фамилия: *{row['last_name']}*\n"
            f"👤 Имя: *{row['first_name']}*\n"
            f"🏫 Класс: *{row['class_name']}*\n\n"
            "✅ Вы будете получать результаты проверок автоматически."
        )
    else:
        text = (
            "❌ *Привязка не найдена*\n\n"
            "Нажмите «Привязать имя», чтобы зарегистрироваться."
        )
    if message_id:
        edit_reply(chat_id, message_id, text, back_keyboard())
    else:
        reply(chat_id, text, back_keyboard())


def do_unlink(chat_id: int, message_id: int | None = None) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_name, first_name, class_name FROM telegram_bindings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM telegram_bindings WHERE chat_id = ?", (chat_id,))
            text = (
                f"✅ Привязка удалена.\n"
                f"Был зарегистрирован как: *{row['last_name']} {row['first_name']} {row['class_name']}*"
            )
            log.info("Отвязка: chat_id=%s", chat_id)
        else:
            text = "ℹ️ Привязка не была установлена."

    if message_id:
        edit_reply(chat_id, message_id, text, back_keyboard())
    else:
        reply(chat_id, text, back_keyboard())


def process_link_input(chat_id: int, text: str) -> None:
    """Обработать ввод «Фамилия Имя Класс» после запроса."""
    parts = text.strip().split(None, 2)
    if len(parts) < 3 or not all(p.strip() for p in parts):
        reply(
            chat_id,
            "⚠️ Нужно три значения: *Фамилия Имя Класс*\n\n"
            "Пример: `Иванов Алексей 8А`\n\n"
            "Попробуйте ещё раз или нажмите кнопку ниже.",
            back_keyboard(),
        )
        return

    last_name  = parts[0].strip()[:60]
    first_name = parts[1].strip()[:60]
    class_name = parts[2].strip()[:20]
    student_reference = f"{last_name} {first_name} {class_name}"
    lookup_key = make_lookup_key(last_name, first_name, class_name)
    ts = now_iso()

    with get_conn() as conn:
        conn.execute("DELETE FROM telegram_bindings WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM telegram_bindings WHERE lookup_key = ?", (lookup_key,))
        conn.execute(
            "INSERT INTO telegram_bindings "
            "(id, student_reference, last_name, first_name, class_name, lookup_key, chat_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id(), student_reference, last_name, first_name, class_name, lookup_key, chat_id, ts, ts),
        )

    _waiting.pop(chat_id, None)
    reply(
        chat_id,
        f"✅ *Готово! Вы зарегистрированы:*\n\n"
        f"👤 {last_name} {first_name}\n"
        f"🏫 Класс: {class_name}\n\n"
        "После каждой проверки работы вы получите разбор здесь.",
        main_menu_keyboard(),
    )
    log.info("Привязка: chat_id=%s → «%s»", chat_id, student_reference)


# ---------------------------------------------------------------------------
# Command & callback handlers
# ---------------------------------------------------------------------------
def handle_command(chat_id: int, text: str) -> None:
    command = text.split()[0].split("@")[0].lower()

    if command in ("/start", "/help", "/menu"):
        _waiting.pop(chat_id, None)
        show_menu(chat_id)
    elif command == "/status":
        show_status(chat_id)
    elif command == "/unlink":
        do_unlink(chat_id)
    elif command == "/link":
        # Поддержка прямой команды: /link Иванов Алексей 8А
        args = text.split(None, 1)
        if len(args) > 1:
            process_link_input(chat_id, args[1])
        else:
            _waiting[chat_id] = "link"
            reply(chat_id, LINK_PROMPT, back_keyboard())
    else:
        reply(chat_id, "Неизвестная команда. Нажмите /start для меню.")


def handle_callback(callback: dict) -> None:
    callback_id: str = callback["id"]
    chat_id: int     = callback["message"]["chat"]["id"]
    message_id: int  = callback["message"]["message_id"]
    data: str        = callback.get("data", "")

    answer_callback(callback_id)
    _waiting.pop(chat_id, None)

    if data == "action:menu":
        edit_reply(chat_id, message_id, WELCOME, main_menu_keyboard())

    elif data == "action:status":
        show_status(chat_id, message_id)

    elif data == "action:unlink":
        do_unlink(chat_id, message_id)

    elif data == "action:help":
        edit_reply(chat_id, message_id, HELP_TEXT, back_keyboard())

    elif data == "action:link":
        _waiting[chat_id] = "link"
        edit_reply(chat_id, message_id, LINK_PROMPT, back_keyboard())


def handle_message(message: dict) -> None:
    chat_id: int = message["chat"]["id"]
    text: str    = (message.get("text") or "").strip()
    if not text:
        return

    # Команда
    if text.startswith("/"):
        handle_command(chat_id, text)
        return

    # Ожидаем ввод имени
    if _waiting.get(chat_id) == "link":
        process_link_input(chat_id, text)
        return

    # Произвольный текст — показать меню
    show_menu(chat_id)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------
def run() -> None:
    init_db()

    try:
        me = _call("getMe")
        bot_name = me["result"].get("username", "?")
        log.info("Бот запущен: @%s", bot_name)
        log.info("Нажмите Ctrl+C для остановки")
    except Exception as e:
        log.error("Не удалось подключиться к Telegram: %s", e)
        log.error("Проверьте правильность TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    offset: int | None = None
    running = True

    def stop(_sig, _frame):
        nonlocal running
        log.info("Остановка...")
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while running:
        try:
            params: dict = {
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            }
            if offset is not None:
                params["offset"] = offset

            data = _call("getUpdates", **params)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "message" in update:
                        handle_message(update["message"])
                    elif "callback_query" in update:
                        handle_callback(update["callback_query"])
                except Exception as e:
                    log.error("Ошибка обработки update: %s", e, exc_info=True)

        except httpx.TimeoutException:
            pass
        except Exception as e:
            if running:
                log.warning("Ошибка опроса: %s — повтор через 5 сек", e)
                time.sleep(5)

    log.info("Бот остановлен.")


if __name__ == "__main__":
    run()

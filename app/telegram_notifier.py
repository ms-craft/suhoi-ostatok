"""Telegram notification helper used by the background worker.

Sends a formatted error-analysis message to a student after their work has
been reviewed.  The function is intentionally fire-and-forget: failures are
logged but never re-raised so that the main processing pipeline is not
affected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger("stopkadr")

# Telegram Bot API base
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Status labels shown to the student
_STATUS_EMOJI = {
    "correct": "✅",
    "incorrect": "❌",
    "partially_correct": "🟡",
    "needs_review": "🔍",
    "no_answer": "⬜",
    "unreadable": "❓",
}

_STATUS_LABEL = {
    "correct": "Верно",
    "incorrect": "Неверно",
    "partially_correct": "Частично верно",
    "needs_review": "Требует проверки",
    "no_answer": "Нет ответа",
    "unreadable": "Не удалось прочитать",
}


def _fmt_task(index: int, task: dict) -> str:
    """Format a single detected task into a human-readable string block."""
    recognition: dict = task.get("recognition") or {}
    preliminary: dict = task.get("preliminary") or {}
    teacher: dict | None = task.get("teacher")

    # Determine final status (teacher override or AI result)
    if teacher and teacher.get("final_status"):
        status = teacher["final_status"]
    elif teacher and teacher.get("status"):
        status = teacher["status"]
    else:
        status = preliminary.get("status", "needs_review")

    emoji = _STATUS_EMOJI.get(status, "❓")
    label = _STATUS_LABEL.get(status, status)

    number = recognition.get("number") or str(index)
    condition_raw = (teacher or {}).get("condition") or recognition.get("condition") or ""
    condition = (condition_raw[:120] + "…") if len(condition_raw) > 120 else condition_raw

    student_answer = (teacher or {}).get("answer") or recognition.get("answer") or "—"
    student_answer = (student_answer[:120] + "…") if len(student_answer) > 120 else student_answer

    if teacher and teacher.get("explanation"):
        explanation = teacher["explanation"]
    else:
        explanation = preliminary.get("explanation") or ""
    explanation = (explanation[:300] + "…") if len(explanation) > 300 else explanation

    expected = (teacher or {}).get("answer") or preliminary.get("expected_answer")

    lines = [
        f"{emoji} *Задание {number}* — {label}",
    ]
    if condition:
        lines.append(f"📋 {_escape(condition)}")
    lines.append(f"✏️ Ваш ответ: `{_escape(student_answer)}`")
    if expected and status not in ("correct",):
        exp_short = (expected[:100] + "…") if len(expected) > 100 else expected
        lines.append(f"🎯 Правильный ответ: `{_escape(exp_short)}`")
    if explanation:
        lines.append(f"💬 {_escape(explanation)}")
    return "\n".join(lines)


def _escape(text: str) -> str:
    """Minimal MarkdownV2 escaping for inline text segments."""
    # We use Markdown (v1) mode so only * ` _ [ need escaping inside normal text.
    for ch in ("\\", "`", "*", "_", "[", "]", "(", ")"):
        text = text.replace(ch, "\\" + ch)
    return text


def build_message(submission: dict, tasks: list[dict]) -> str:
    """Build the full notification message text."""
    student = submission.get("student_reference") or "Ученик"
    created = submission.get("created_at")
    date_str = ""
    if created:
        try:
            from datetime import datetime, timezone
            if isinstance(created, str):
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                dt = created
            # Moscow time offset +3
            from datetime import timedelta
            msk = dt.astimezone(timezone(timedelta(hours=3)))
            date_str = f" от {msk.strftime('%d.%m.%Y')}"
        except Exception:
            pass

    total = len(tasks)
    correct = sum(
        1 for t in tasks
        if (t.get("teacher") or {}).get("final_status") == "correct"
        or (not (t.get("teacher") or {}).get("final_status")
            and t.get("preliminary", {}).get("status") == "correct")
    )

    header = (
        f"📝 *Результаты проверки работы{date_str}*\n"
        f"👤 {_escape(student)}\n"
        f"✅ Верно: {correct}/{total}\n"
    )
    task_blocks = "\n\n".join(_fmt_task(i + 1, t) for i, t in enumerate(tasks))
    footer = (
        "\n\n—\n"
        "_Это автоматическое сообщение от системы проверки «Сухой остаток»._"
    )
    return header + "\n" + task_blocks + footer


def send_review_notification(
    bot_token: str,
    chat_id: int,
    submission: dict,
    tasks: list[dict],
    *,
    timeout: float = 10.0,
) -> None:
    """Send the formatted review to *chat_id* via Telegram Bot API (synchronous).

    Raises nothing — all errors are caught and logged.
    """
    try:
        text = build_message(submission, tasks)
        url = _TG_API.format(token=bot_token)
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        resp = httpx.post(url, json=payload, timeout=timeout)
        if not resp.is_success:
            logger.warning(
                "telegram_send_failed",
                extra={"event_fields": {
                    "component": "telegram",
                    "chat_id": chat_id,
                    "status_code": resp.status_code,
                    "response": resp.text[:200],
                }},
            )
        else:
            logger.info(
                "telegram_notification_sent",
                extra={"event_fields": {"component": "telegram", "chat_id": chat_id}},
            )
    except Exception as exc:
        logger.warning(
            "telegram_send_error",
            extra={"event_fields": {
                "component": "telegram",
                "chat_id": chat_id,
                "error": str(exc)[:200],
            }},
        )
        raise


def notify_student_after_review(
    state,
    submission_id: str,
    student_reference: str | None,
    tasks: list[dict],
    submission_meta: dict,
) -> None:
    """Look up binding and dispatch notification.  Called from the worker after _persist."""
    if not getattr(state.settings, "telegram_bot_token", None):
        return  # Telegram not configured — silently skip
    if not student_reference:
        return  # No name label → no binding possible

    bot_token: str = state.settings.telegram_bot_token.get_secret_value()

    # Resolve chat_id from DB
    from sqlalchemy import select
    from app.telegram_models import TelegramBinding, TelegramNotification

    try:
        with state.session_factory() as db:
            binding = db.scalar(
                select(TelegramBinding).where(
                    TelegramBinding.student_reference == student_reference
                )
            )
        if binding is None:
            return  # Student hasn't linked their Telegram yet
        chat_id = binding.chat_id
    except Exception as exc:
        logger.warning(
            "telegram_binding_lookup_failed",
            extra={"event_fields": {
                "component": "telegram",
                "submission_id": submission_id,
                "error": str(exc)[:200],
            }},
        )
        return

    # Send and log outcome
    status = "sent"
    error_msg = None
    try:
        send_review_notification(bot_token, chat_id, submission_meta, tasks)
    except Exception as exc:
        status = "failed"
        error_msg = str(exc)[:256]

    try:
        from app.core import new_id, utcnow
        with state.session_factory.begin() as db:
            db.add(TelegramNotification(
                id=new_id(),
                submission_id=submission_id,
                chat_id=chat_id,
                status=status,
                error=error_msg,
            ))
    except Exception as exc:
        logger.warning(
            "telegram_notification_log_failed",
            extra={"event_fields": {
                "component": "telegram",
                "submission_id": submission_id,
                "error": str(exc)[:200],
            }},
        )

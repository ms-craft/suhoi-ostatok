"""ORM models for Telegram bot: student ↔ chat_id binding."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core import new_id, utcnow
from app.db import Base


class TelegramBinding(Base):
    """Links a student_reference label (set by teacher) to a Telegram chat_id.

    One student_reference may be bound to exactly one chat.  A single chat can
    also re-bind to a different name (old row is replaced).
    """

    __tablename__ = "telegram_bindings"
    __table_args__ = (
        UniqueConstraint("student_reference", name="uq_telegram_bindings_reference"),
        UniqueConstraint("chat_id", name="uq_telegram_bindings_chat"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # The label the teacher types in "student_reference" field, e.g. "Иванов 8А"
    student_reference: Mapped[str] = mapped_column(String(120))
    # Telegram chat identifier (positive for private chats)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TelegramNotification(Base):
    """Audit log of every notification attempt sent by the bot."""

    __tablename__ = "telegram_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True,
    )
    chat_id: Mapped[int] = mapped_column(BigInteger)
    # "sent" | "failed"
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

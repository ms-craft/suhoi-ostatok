"""Class-scoped Telegram registration and durable result delivery."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core import new_id, utcnow
from app.db import Base


class TelegramClass(Base):
    __tablename__ = 'telegram_classes'
    __table_args__ = (UniqueConstraint('user_id', 'external_id', name='uq_telegram_class_owner_external'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    external_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    code_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramBinding(Base):
    __tablename__ = 'telegram_bindings'
    __table_args__ = (UniqueConstraint('class_id', 'name_key', name='uq_telegram_class_name'),
                      UniqueConstraint('class_id', 'chat_id', name='uq_telegram_class_chat'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    class_id: Mapped[str] = mapped_column(ForeignKey('telegram_classes.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(120))
    name_key: Mapped[str] = mapped_column(String(240))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramDelivery(Base):
    __tablename__ = 'telegram_deliveries'
    __table_args__ = (UniqueConstraint('submission_id', 'binding_id', name='uq_telegram_delivery'),
                      CheckConstraint("status IN ('pending','sent','failed')", name='ck_telegram_delivery_status'),
                      Index('ix_telegram_delivery_status', 'status'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey('submissions.id', ondelete='CASCADE'))
    binding_id: Mapped[str] = mapped_column(ForeignKey('telegram_bindings.id', ondelete='CASCADE'))
    status: Mapped[str] = mapped_column(String(16), default='pending')
    digest: Mapped[str | None] = mapped_column(String(64))
    inflight_digest: Mapped[str | None] = mapped_column(String(64))
    sent_parts: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

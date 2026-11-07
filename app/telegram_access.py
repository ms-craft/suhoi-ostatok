"""Teacher-controlled class codes and class-scoped student matching."""
import hashlib
import re
import secrets
import unicodedata
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import current_user
from app.billing import lock_teacher
from app.core import ApiError, utcnow
from app.db import get_db
from app.submission_models import Submission
from app.telegram_models import TelegramBinding, TelegramClass, TelegramDelivery

router = APIRouter(prefix='/telegram', tags=['Telegram'])
Owner = Annotated[str, Depends(current_user)]
DB = Annotated[Session, Depends(get_db)]


def name_key(name: str | None) -> str:
    parts = unicodedata.normalize('NFKC', name or '').casefold().split()
    # First name + surname may be written in either order on a sheet.
    if len(parts) == 2:
        parts.sort()
    return ' '.join(parts)


def valid_name(name: str) -> bool:
    words = name.split()
    return 2 <= len(words) <= 4 and len(name) <= 120 and all(
        any(char.isalpha() for char in word) and all(char.isalpha() or char in "-'’" for char in word)
        for word in words
    )


def schedule_submission(db: Session, row: Submission) -> None:
    existing = list(db.scalars(select(TelegramDelivery).where(TelegramDelivery.submission_id == row.id)))
    if not row.class_reference or not row.student_reference or row.delete_requested_at is not None:
        for item in existing:
            db.delete(item)
        return
    cls = db.scalar(select(TelegramClass).where(TelegramClass.user_id == row.user_id,
                                                TelegramClass.external_id == row.class_reference))
    if cls is None:
        for item in existing:
            db.delete(item)
        return
    binding = db.scalar(select(TelegramBinding).where(TelegramBinding.class_id == cls.id,
                                                      TelegramBinding.name_key == name_key(row.student_reference)))
    for item in existing:
        if binding is None or item.binding_id != binding.id:
            db.delete(item)
    if binding is None:
        return
    delivery = next((item for item in existing if item.binding_id == binding.id), None)
    if delivery is None:
        db.add(TelegramDelivery(submission_id=row.id, binding_id=binding.id))
    else:
        delivery.status = 'pending'
        delivery.attempts = 0
        delivery.next_attempt_at = utcnow()


class ClassCodeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    submission_ids: list[str] = Field(default_factory=list, max_length=1000)


@router.post('/classes/{class_reference}/code')
def create_class_code(class_reference: str, body: ClassCodeRequest, request: Request, db: DB, user_id: Owner):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', class_reference):
        raise ApiError(422, 'invalid_class', 'Некорректный класс.')
    lock_teacher(db, user_id)
    cls = db.scalar(select(TelegramClass).where(TelegramClass.user_id == user_id,
                                                TelegramClass.external_id == class_reference))
    code = secrets.token_urlsafe(12)
    digest = hashlib.sha256(code.encode('ascii')).hexdigest()
    if cls is None:
        cls = TelegramClass(user_id=user_id, external_id=class_reference, name=body.name, code_digest=digest)
        db.add(cls)
        db.flush()
    else:
        cls.name, cls.code_digest = body.name, digest
    # Older browser-local work links are migrated only when the teacher explicitly creates a class code.
    if body.submission_ids:
        rows = list(db.scalars(select(Submission).where(Submission.user_id == user_id,
                                                        Submission.id.in_(body.submission_ids),
                                                        Submission.delete_requested_at.is_(None))))
        for row in rows:
            if row.class_reference is None or row.class_reference == class_reference:
                row.class_reference = class_reference
                if row.first_completed_at is not None:
                    schedule_submission(db, row)
    db.commit()
    return {'code': code, 'bot_configured': bool(request.app.state.settings.telegram_bot_token)}


@router.get('/classes/{class_reference}')
def class_status(class_reference: str, request: Request, db: DB, user_id: Owner):
    cls = db.scalar(select(TelegramClass).where(TelegramClass.user_id == user_id,
                                                TelegramClass.external_id == class_reference))
    bindings = db.scalars(select(TelegramBinding).where(TelegramBinding.class_id == cls.id)
                          .order_by(TelegramBinding.name)).all() if cls else []
    return {'exists': cls is not None, 'bot_configured': bool(request.app.state.settings.telegram_bot_token),
            'students': [{'id': item.id, 'name': item.name} for item in bindings]}


@router.delete('/classes/{class_reference}/students/{binding_id}', status_code=204)
def remove_student(class_reference: str, binding_id: str, db: DB, user_id: Owner):
    lock_teacher(db, user_id)
    binding = db.scalar(select(TelegramBinding).join(TelegramClass).where(
        TelegramClass.user_id == user_id, TelegramClass.external_id == class_reference,
        TelegramBinding.id == binding_id))
    if binding is None:
        raise ApiError(404, 'student_not_found', 'Регистрация ученика не найдена.')
    db.delete(binding)
    db.commit()
    return Response(status_code=204)


@router.delete('/classes/{class_reference}', status_code=204)
def delete_class_code(class_reference: str, db: DB, user_id: Owner):
    lock_teacher(db, user_id)
    cls = db.scalar(select(TelegramClass).where(TelegramClass.user_id == user_id,
                                                TelegramClass.external_id == class_reference))
    if cls:
        db.delete(cls)
    db.commit()
    return Response(status_code=204)


@router.get('/submissions/{submission_id}/delivery')
def delivery_status(submission_id: UUID, request: Request, db: DB, user_id: Owner):
    row = db.get(Submission, str(submission_id))
    if row is None or row.user_id != user_id or row.delete_requested_at is not None:
        raise ApiError(404, 'not_found', 'Работа не найдена.')
    configured = bool(request.app.state.settings.telegram_bot_token)
    if not row.class_reference:
        return {'status': 'no_class', 'bot_configured': configured}
    cls = db.scalar(select(TelegramClass).where(TelegramClass.user_id == user_id,
                                                TelegramClass.external_id == row.class_reference))
    if cls is None:
        return {'status': 'no_class_code', 'bot_configured': configured}
    binding = db.scalar(select(TelegramBinding).where(TelegramBinding.class_id == cls.id,
                                                      TelegramBinding.name_key == name_key(row.student_reference)))
    if binding is None:
        return {'status': 'student_not_linked', 'bot_configured': configured}
    delivery = db.scalar(select(TelegramDelivery).where(TelegramDelivery.submission_id == row.id,
                                                        TelegramDelivery.binding_id == binding.id))
    return {'status': delivery.status if delivery else 'not_scheduled', 'bot_configured': configured}


@router.post('/submissions/{submission_id}/retry-delivery')
def retry_delivery(submission_id: UUID, db: DB, user_id: Owner):
    row = db.scalar(select(Submission).where(Submission.id == str(submission_id),
                                            Submission.user_id == user_id,
                                            Submission.delete_requested_at.is_(None)).with_for_update())
    if row is None:
        raise ApiError(404, 'not_found', 'Работа не найдена.')
    cls = db.scalar(select(TelegramClass).where(TelegramClass.user_id == user_id,
                                                TelegramClass.external_id == row.class_reference))
    binding = db.scalar(select(TelegramBinding).where(TelegramBinding.class_id == cls.id,
                         TelegramBinding.name_key == name_key(row.student_reference))) if cls else None
    delivery = db.scalar(select(TelegramDelivery).where(TelegramDelivery.submission_id == row.id,
                                                        TelegramDelivery.binding_id == binding.id).with_for_update()) if binding else None
    if delivery is None or delivery.status != 'failed':
        raise ApiError(409, 'delivery_not_failed', 'Повтор доступен после ошибки отправки.')
    delivery.status = 'pending'
    delivery.attempts = 0
    delivery.next_attempt_at = utcnow()
    db.commit()
    return {'status': 'pending'}

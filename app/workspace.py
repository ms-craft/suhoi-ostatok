"""Versioned teacher workspace, shared between the teacher's devices."""
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.accounts import CurrentUser, Db
from app.billing import lock_teacher
from app.core import ApiError, utcnow
from app.db import Base

router = APIRouter(tags=['Рабочее пространство'])


class TeacherWorkspace(Base):
    __tablename__ = 'teacher_workspaces'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    workspace: dict[str, Any]


def validate_workspace(data: dict) -> None:
    if set(data) != {'classes', 'tests', 'links'} or not isinstance(data['classes'], list) or not isinstance(data['tests'], list) or not isinstance(data['links'], dict):
        raise ApiError(422, 'invalid_workspace', 'Проверьте данные рабочего пространства.')
    if len(data['classes']) > 1000 or len(data['tests']) > 1000 or len(data['links']) > 20000:
        raise ApiError(413, 'workspace_too_large', 'Слишком много данных в рабочем пространстве.')
    if len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > 2_000_000:
        raise ApiError(413, 'workspace_too_large', 'Рабочее пространство слишком большое.')
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('name'), str)
           or not item['id'] or len(item['id']) > 100 or not item['name'] or len(item['name']) > 100
           for item in data['classes'] + data['tests']):
        raise ApiError(422, 'invalid_workspace', 'Проверьте названия классов и работ.')


@router.get('/workspace')
def get_workspace(db: Db, user_id: CurrentUser):
    row = db.get(TeacherWorkspace, user_id)
    return {'revision': row.revision if row else 0, 'workspace': row.data if row else None}


@router.put('/workspace')
def put_workspace(body: WorkspaceUpdate, db: Db, user_id: CurrentUser):
    validate_workspace(body.workspace)
    lock_teacher(db, user_id)
    row = db.get(TeacherWorkspace, user_id)
    if (row.revision if row else 0) != body.revision:
        raise ApiError(409, 'workspace_conflict', 'Данные изменились на другом устройстве.')
    if row is None:
        row = TeacherWorkspace(user_id=user_id, revision=1, data=body.workspace)
        db.add(row)
    else:
        row.revision += 1
        row.data = body.workspace
    db.commit()
    return {'revision': row.revision}

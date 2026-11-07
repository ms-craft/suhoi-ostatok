"""School subject suggestions stored in the application database."""
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.accounts import CurrentUser, Db
from app.db import Base

router = APIRouter(tags=['subjects'])


class Subject(Base):
    __tablename__ = 'subjects'
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    category: Mapped[str] = mapped_column(String(16))


class SubjectView(BaseModel):
    name: str
    category: Literal['stem', 'humanities']


@router.get('/subjects', response_model=list[SubjectView])
def list_subjects(db: Db, user_id: CurrentUser, q: Annotated[str, Query(max_length=100)] = ''):
    needle = q.strip().casefold()
    rows = db.scalars(select(Subject).order_by(Subject.name)).all()
    return [SubjectView(name=row.name, category=row.category) for row in rows if needle in row.name.casefold()][:50]

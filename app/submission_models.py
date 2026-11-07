from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core import new_id, utcnow
from app.db import Base

JOB_STATUSES = "('queued','processing','completed','failed')"


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint(f"status in {JOB_STATUSES}", name="ck_submissions_status"),
        Index("ix_submissions_owner_created", "user_id", "created_at", "id"),
        Index("ix_submissions_cleanup", "delete_requested_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    latest_job_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    student_reference: Mapped[str | None] = mapped_column(String(120))
    first_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceFile(Base):
    __tablename__ = "submission_files"
    __table_args__ = (UniqueConstraint("submission_id", "number", name="uq_submission_file_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(220), unique=True)
    mime_type: Mapped[str] = mapped_column(String(32))


class SubmissionPage(Base):
    __tablename__ = "submission_pages"
    __table_args__ = (
        UniqueConstraint("submission_id", "number", name="uq_submission_page_number"),
        CheckConstraint("number >= 1 and source_page >= 1", name="ck_submission_page_numbers"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    source_file_id: Mapped[str] = mapped_column(ForeignKey("submission_files.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    source_page: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(220), unique=True)


class DetectedTask(Base):
    __tablename__ = "detected_tasks"
    __table_args__ = (Index("ix_detected_tasks_submission_order", "submission_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int] = mapped_column(Integer)
    original_recognition: Mapped[dict] = mapped_column(JSON)
    original_preliminary: Mapped[dict] = mapped_column(JSON)
    recognition: Mapped[dict] = mapped_column(JSON)
    preliminary: Mapped[dict] = mapped_column(JSON)
    teacher: Mapped[dict | None] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(32))
    demonstration: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(f"status in {JOB_STATUSES}", name="ck_processing_jobs_status"),
        CheckConstraint("kind in ('initial','clarification','recheck')", name="ck_processing_jobs_kind"),
        CheckConstraint("attempts >= 0", name="ck_processing_jobs_attempts"),
        Index("ix_processing_jobs_due", "status", "next_attempt_at", "lease_until"),
        Index("ix_processing_jobs_submission_created", "submission_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("detected_tasks.id", ondelete="CASCADE"), index=True,
    )
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reservation_id: Mapped[str | None] = mapped_column(ForeignKey("usage_records.id"), unique=True)
    origin_request_id: Mapped[str | None] = mapped_column(String(36))
    clarification: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(32))
    demonstration: Mapped[bool | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskAnalysis(Base):
    __tablename__ = "task_analyses"
    __table_args__ = (UniqueConstraint("task_id", "job_id", name="uq_task_analysis_job"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("detected_tasks.id", ondelete="CASCADE"))
    job_id: Mapped[str] = mapped_column(ForeignKey("processing_jobs.id", ondelete="CASCADE"), index=True)
    recognition: Mapped[dict] = mapped_column(JSON)
    review_input: Mapped[dict] = mapped_column(JSON)
    preliminary: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TeacherEdit(Base):
    __tablename__ = "teacher_edits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("detected_tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

from datetime import datetime
from io import BytesIO
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.accounts import current_user
from app.ai import PreliminaryReview, RecognizedTask, ReviewStatus
from app.billing import Entitlement, entitlement, reserve_check, reserve_sheets, settle_check
from app.core import ApiError, as_utc, event, new_id, request_id, utcnow
from app.db import get_db
from app.files import HEIC, HEIF, JPEG, PreparedFile, normalized_jpeg, page_limit, prepare_uploads
from app.object_lock import lock_object_writer
from app.submission_models import DetectedTask, ProcessingJob, SourceFile, Submission, SubmissionPage, TeacherEdit
from app.telegram_access import schedule_submission

router = APIRouter(prefix="/submissions", tags=["Работы"])
DB = Annotated[Session, Depends(get_db)]
Owner = Annotated[str, Depends(current_user)]
BUSY = {"queued", "processing"}


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    number: str | None = Field(None, max_length=80, pattern=r'^[1-9][0-9]*$')
    condition: str | None = Field(None, max_length=8000)
    answer: str | None = Field(None, max_length=16000)
    status: ReviewStatus | None = None
    explanation: str | None = Field(None, max_length=4000)


JobStatus = Literal["queued", "processing", "completed", "failed"]


class SubmissionView(BaseModel):
    id: str
    job_id: str
    status: JobStatus
    stage: str
    error_code: str | None
    error_message: str | None
    page_count: int
    student_reference: str | None
    class_reference: str | None = None
    created_at: datetime


class SubmissionList(BaseModel):
    items: list[SubmissionView]


class SubmissionLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    student_reference: str | None = Field(max_length=120)


class TestQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=8000)
    answer: str = Field(min_length=1, max_length=3000)


class JobView(BaseModel):
    id: str
    status: JobStatus
    stage: str
    attempts: int
    kind: str
    error_code: str | None
    error_message: str | None


class TeacherView(BaseModel):
    number: str | None = None
    condition: str | None = None
    answer: str | None = None
    status: ReviewStatus | None = None
    explanation: str | None = None
    confirmed_at: datetime | None = None
    final_status: ReviewStatus | None = None


class TaskView(BaseModel):
    id: str
    recognition: RecognizedTask
    preliminary: PreliminaryReview
    original_recognition: RecognizedTask
    original_preliminary: PreliminaryReview
    source_page: int
    source_url: str
    provider: str
    demonstration: bool
    teacher: TeacherView | None
    confirmed: bool


class ResultsView(BaseModel):
    id: str
    demonstration: bool
    provider: str | None
    tasks: list[TaskView]


def missing() -> ApiError:
    return ApiError(404, "not_found", "Работа, страница или задание не найдены.")


def lock_submission(db: Session, submission_id: str, user_id: str | None = None) -> Submission | None:
    filters = [Submission.id == submission_id]
    if user_id is not None:
        filters.append(Submission.user_id == user_id)
    if db.get_bind().dialect.name == "sqlite":
        # SQLite has no row lock. Obtain its writer lock before reading mutable state.
        db.execute(update(Submission).where(*filters).values(id=Submission.id))
    return db.scalar(select(Submission).where(*filters).with_for_update())


def owned(db: Session, submission_id: UUID, user_id: str, *, lock: bool = False) -> Submission:
    row = lock_submission(db, str(submission_id), user_id) if lock else db.get(Submission, str(submission_id))
    if row is None or row.user_id != user_id or row.delete_requested_at is not None:
        raise missing()
    return row


def task_for(db: Session, row: Submission, task_id: UUID) -> DetectedTask:
    task = db.get(DetectedTask, str(task_id))
    if task is None or task.submission_id != row.id:
        raise missing()
    return task


def check_idle(row: Submission) -> None:
    if row.status in BUSY:
        raise ApiError(409, "job_in_progress", "Дождитесь завершения фоновой обработки.")


def check_followups(db: Session, user_id: str, *, sheets: bool = False, max_pages: int = 100):
    limits = Entitlement(max_pages=max_pages, allow_followups=True) if sheets else entitlement(db, user_id)
    if not limits.allow_followups:
        raise ApiError(403, "followups_unavailable", "Тариф не включает уточнения и повторную проверку.")
    return limits


def processing_message(code: str | None) -> str | None:
    if code is None:
        return None
    if code == "ai_unavailable":
        return "Не удалось получить ответ ИИ-провайдера. Повторный запуск отправит новый запрос, который может быть платным."
    if code.startswith("ai_"):
        return "ИИ-провайдер не смог подготовить пригодный результат."
    if code == "storage_unavailable":
        return "Хранилище страниц временно недоступно."
    return "Фоновая обработка прервана."


def submission_data(row: Submission) -> dict:
    return {
        "id": row.id,
        "job_id": row.latest_job_id,
        "status": row.status,
        "stage": row.stage,
        "error_code": row.error_code,
        "error_message": processing_message(row.error_code),
        "page_count": row.page_count,
        "student_reference": row.student_reference,
        "class_reference": row.class_reference,
        "created_at": as_utc(row.created_at),
    }


def job_data(job: ProcessingJob) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "attempts": job.attempts,
        "kind": job.kind,
        "error_code": job.error_code,
        "error_message": processing_message(job.error_code),
    }


def task_data(task: DetectedTask) -> dict:
    return {
        "id": task.id,
        "recognition": task.recognition,
        "preliminary": task.preliminary,
        "original_recognition": task.original_recognition,
        "original_preliminary": task.original_preliminary,
        "source_page": task.page_number,
        "source_url": f"/v1/submissions/{task.submission_id}/pages/{task.page_number}",
        "provider": task.provider,
        "demonstration": task.demonstration,
        "teacher": task.teacher,
        "confirmed": bool(task.teacher and task.teacher.get("confirmed_at")),
    }


def enqueue(db: Session, row: Submission, kind: str, *, task_id=None, clarification=None,
            reservation_id=None, job_id=None) -> ProcessingJob:
    job = ProcessingJob(
        id=job_id or new_id(),
        submission_id=row.id,
        kind=kind,
        task_id=task_id,
        clarification=clarification,
        reservation_id=reservation_id,
        origin_request_id=request_id.get(),
    )
    db.add(job)
    row.latest_job_id = job.id
    row.status, row.stage, row.error_code = "queued", "queued", None
    return job


def notify(state, job_id: str, submission_id: str, user_id: str) -> None:
    try:
        state.queue.push(job_id)
    except Exception:
        event("job_signal_failed", level="WARNING", job_id=job_id, submission_id=submission_id,
              user_id=user_id, error_code="redis_unavailable")
    else:
        event("job_enqueued", job_id=job_id, submission_id=submission_id, user_id=user_id)


def save_uploads(db: Session, state, row: Submission, files: list[PreparedFile]) -> None:
    if not files:
        db.commit()
        return
    lock_object_writer(db)
    last_number = db.scalar(
        select(SourceFile.number).where(SourceFile.submission_id == row.id)
        .order_by(SourceFile.number.desc()).limit(1)
    ) or 0
    written: list[str] = []
    batch_id = new_id()
    submission_id = row.id
    prefix = f"submissions/{submission_id}/{batch_id}"

    def write(key: str, content: bytes, mime: str) -> None:
        written.append(key)
        state.storage.put(key, content, mime)

    try:
        for number, file in enumerate(files, last_number + 1):
            source_id = new_id()
            key = f"{prefix}/original/{number}"
            write(key, file.content, file.mime_type)
            db.add(SourceFile(
                id=source_id, submission_id=row.id, number=number, object_key=key, mime_type=file.mime_type,
            ))
            db.flush()
            for index, page in enumerate(file.pages, 1):
                row.page_count += 1
                key = f"{prefix}/pages/{row.page_count}.jpg"
                write(key, page, JPEG)
                db.add(SubmissionPage(
                    submission_id=row.id, source_file_id=source_id, number=row.page_count,
                    source_page=index, object_key=key,
                ))
        db.flush()
    except Exception as error:
        db.rollback()
        for key in written:
            try:
                state.storage.delete(key)
            except Exception:
                event("upload_rollback_cleanup_failed", level="ERROR", submission_id=submission_id,
                      error_code="storage_unavailable")
        raise ApiError(503, "upload_unavailable", "Сохранение файлов временно недоступно.") from error
    try:
        db.commit()
    except Exception as error:
        # A lost database connection can hide a successful commit. Keep the objects
        # because a committed job may already point to them.
        db.rollback()
        event("upload_commit_outcome_unknown", level="ERROR", submission_id=submission_id,
              error_code="database_unavailable")
        raise ApiError(503, "upload_unavailable", "Подтверждение загрузки временно недоступно.") from error


@router.post('/preview', response_class=Response)
def preview_heic(request: Request, user_id: Owner, file: Annotated[UploadFile, File()]):
    if file.content_type not in {None, 'application/octet-stream', HEIC, HEIF}:
        raise ApiError(415, 'unsupported_file', 'Для предпросмотра выберите HEIC или HEIF.')
    prepared = prepare_uploads([file], request.app.state.settings, max_pages=1)
    if prepared[0].mime_type not in {HEIC, HEIF}:
        raise ApiError(415, 'unsupported_file', 'Для предпросмотра выберите HEIC или HEIF.')
    with Image.open(BytesIO(prepared[0].pages[0]), formats=['JPEG']) as image:
        thumbnail = normalized_jpeg(image, 480)
    return Response(thumbnail, media_type=JPEG,
                    headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


@router.post("", status_code=202, response_model=SubmissionView)
def upload(
    request: Request, db: DB, user_id: Owner,
    files: Annotated[list[UploadFile] | None, File()] = None,
    test_questions: Annotated[str | None, Form(max_length=30000)] = None,
    test_subject: Annotated[str | None, Form(max_length=100)] = None,
    test_subject_category: Annotated[Literal['stem', 'humanities'] | None, Form()] = None,
    student_reference: Annotated[str | None, Form(max_length=120)] = None,
    class_reference: Annotated[str | None, Form(max_length=100, pattern=r'^[A-Za-z0-9_-]+$')] = None,
):
    state = request.app.state
    test_subject = test_subject.strip() if test_subject else None
    if test_subject:
        from app.subjects import Subject
        known = next((item for item in db.scalars(select(Subject)).all() if item.name.casefold() == test_subject.casefold()), None)
        legacy = {'math': 'stem', 'russian': 'humanities', 'belarusian': 'humanities', 'literature': 'humanities', 'history': 'humanities', 'social_studies': 'humanities', 'english': 'humanities', 'other_humanities': 'humanities'}
        test_subject_category = known.category if known else legacy.get(test_subject, 'humanities')
    references = []
    if test_questions:
        try:
            raw = json.loads(test_questions)
            if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
                raise ValueError()
            references = [TestQuestion.model_validate(item).model_dump() for item in raw]
        except (ValueError, TypeError):
            raise ApiError(422, "invalid_test", "Проверьте задания и ответы работы.") from None
    prepared = prepare_uploads(files or [], state.settings, max_pages=state.settings.max_pages)
    sheets = state.settings.sheet_billing_enabled
    page_count = sum(len(file.pages) for file in prepared)
    reservation = reserve_sheets(db, user_id, page_count) if sheets else reserve_check(db, user_id)
    limits = Entitlement(max_pages=state.settings.max_pages, allow_followups=True) if sheets else entitlement(db, user_id)
    if sum(len(file.pages) for file in prepared) > limits.max_pages:
        raise page_limit()

    job_id = new_id()
    row = Submission(id=new_id(), user_id=user_id, latest_job_id=job_id, page_count=0, class_reference=class_reference,
                     student_reference=student_reference.strip() or None if student_reference else None)
    db.add(row)
    db.flush()
    enqueue(db, row, "initial", reservation_id=reservation, job_id=job_id,
            clarification=json.dumps({'questions': references, 'subject': test_subject, 'subject_category': test_subject_category}, ensure_ascii=False)
            if references or test_subject else None)
    save_uploads(db, state, row, prepared)
    event("submission_uploaded", submission_id=row.id, user_id=user_id, job_id=job_id,
          page_count=row.page_count)
    notify(state, job_id, row.id, user_id)
    return submission_data(row)


@router.get("", response_model=SubmissionList)
def list_submissions(
    db: DB, user_id: Owner, limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rows = db.scalars(
        select(Submission).where(Submission.user_id == user_id, Submission.delete_requested_at.is_(None))
        .order_by(Submission.created_at.desc(), Submission.id.desc()).offset(offset).limit(limit)
    )
    return {"items": [submission_data(row) for row in rows]}


@router.get("/{submission_id}", response_model=SubmissionView)
def get_submission(submission_id: UUID, db: DB, user_id: Owner):
    return submission_data(owned(db, submission_id, user_id))


@router.patch("/{submission_id}", response_model=SubmissionView)
def label_submission(submission_id: UUID, body: SubmissionLabel, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id, lock=True)
    row.student_reference = body.student_reference or None
    if row.first_completed_at is not None:
        schedule_submission(db, row)
    db.commit()
    return submission_data(row)


@router.get("/{submission_id}/jobs/{job_id}", response_model=JobView)
def get_job(submission_id: UUID, job_id: UUID, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id)
    job = db.get(ProcessingJob, str(job_id))
    if job is None or job.submission_id != row.id:
        raise missing()
    return job_data(job)


@router.get("/{submission_id}/results", response_model=ResultsView, response_model_exclude_unset=True)
def results(submission_id: UUID, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id)
    if row.first_completed_at is None:
        raise ApiError(409, "results_unavailable", "Результат обработки пока недоступен.")
    latest_success = db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.submission_id == row.id, ProcessingJob.status == "completed",
        ).order_by(ProcessingJob.created_at.desc()).limit(1)
    )
    tasks = db.scalars(
        select(DetectedTask).where(DetectedTask.submission_id == row.id).order_by(DetectedTask.position)
    ).all()
    return {
        "id": row.id,
        "demonstration": bool(latest_success and latest_success.demonstration) or any(
            task.demonstration for task in tasks
        ),
        "provider": latest_success.provider if latest_success else None,
        "tasks": [task_data(task) for task in tasks],
    }


def serve_object(state, key: str, mime: str, *, attachment: bool = False) -> Response:
    try:
        content, _ = state.storage.get(key)
    except Exception as error:
        raise ApiError(503, "storage_unavailable", "Файл временно недоступен.") from error
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if attachment:
        headers["Content-Disposition"] = "attachment"
    return Response(content, media_type=mime, headers=headers)


@router.get("/{submission_id}/pages/{number}", response_class=Response)
def get_page(request: Request, submission_id: UUID, number: int, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id)
    page = db.scalar(select(SubmissionPage).where(
        SubmissionPage.submission_id == row.id, SubmissionPage.number == number,
    ))
    if page is None:
        raise missing()
    return serve_object(request.app.state, page.object_key, JPEG)


@router.get("/{submission_id}/files/{number}", response_class=Response)
def get_original(request: Request, submission_id: UUID, number: int, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id)
    file = db.scalar(select(SourceFile).where(SourceFile.submission_id == row.id, SourceFile.number == number))
    if file is None:
        raise missing()
    return serve_object(request.app.state, file.object_key, file.mime_type, attachment=True)


@router.post("/{submission_id}/clarifications", status_code=202, response_model=SubmissionView)
def clarify(
    request: Request, submission_id: UUID, db: DB, user_id: Owner,
    text: Annotated[str | None, Form(max_length=4000)] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    state = request.app.state
    text = (text or "").strip() or None
    if text is None and not files:
        raise ApiError(422, "missing_clarification", "Добавьте страницу или текст уточнения.")
    owned(db, submission_id, user_id)
    prepared = prepare_uploads(files or [], state.settings, max_pages=state.settings.max_pages, required=False)
    row = owned(db, submission_id, user_id, lock=True)
    check_idle(row)
    sheets = state.settings.sheet_billing_enabled
    limits = check_followups(db, user_id, sheets=sheets, max_pages=state.settings.max_pages)
    if row.page_count + sum(len(file.pages) for file in prepared) > min(
        limits.max_pages, state.settings.max_pages
    ):
        raise page_limit()
    added_pages = sum(len(file.pages) for file in prepared)
    if sheets:
        pages = max(0, row.page_count + added_pages - (row.accounted_pages or 0))
        reservation = reserve_sheets(db, user_id, pages) if pages else None
    else:
        reservation = reserve_check(db, user_id) if row.first_completed_at is None else None
    job = enqueue(db, row, "clarification", clarification=text, reservation_id=reservation)
    save_uploads(db, state, row, prepared)
    event("submission_clarified", submission_id=row.id, user_id=user_id, job_id=job.id,
          page_count=row.page_count)
    notify(state, job.id, row.id, user_id)
    return submission_data(row)


@router.post("/{submission_id}/retry", status_code=202, response_model=SubmissionView)
def retry(request: Request, submission_id: UUID, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id, lock=True)
    if row.status != "failed":
        raise ApiError(409, "job_not_failed", "Повторный запуск доступен для завершившейся с ошибкой задачи.")
    previous = db.get(ProcessingJob, row.latest_job_id)
    sheets = request.app.state.settings.sheet_billing_enabled
    if previous.kind != "initial":
        check_followups(db, user_id, sheets=sheets)
    if sheets:
        pages = max(0, row.page_count - (row.accounted_pages or 0)) if previous.kind != 'recheck' else 0
        reservation = reserve_sheets(db, user_id, pages) if pages else None
    else:
        reservation = reserve_check(db, user_id) if row.first_completed_at is None else None
    if previous.kind != "recheck" and row.page_count > min(
        (request.app.state.settings.max_pages if sheets else entitlement(db, user_id).max_pages), request.app.state.settings.max_pages,
    ):
        raise page_limit()
    job = enqueue(db, row, previous.kind, task_id=previous.task_id, reservation_id=reservation)
    db.commit()
    notify(request.app.state, job.id, row.id, user_id)
    return submission_data(row)


@router.patch("/{submission_id}/tasks/{task_id}", response_model=TaskView, response_model_exclude_unset=True)
def correct(submission_id: UUID, task_id: UUID, body: Correction, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id, lock=True)
    check_idle(row)
    task = task_for(db, row, task_id)
    changes = body.model_dump(mode="json", exclude_unset=True)
    if not changes:
        raise ApiError(422, "missing_correction", "Укажите хотя бы одно исправление.")
    previous_number = (task.teacher or {}).get('number', task.recognition.get('number'))
    if 'number' in changes and changes['number'] != previous_number:
        task.preliminary = PreliminaryReview(
            status='needs_review',
            explanation='Номер задания изменён. Перепроверьте задание, чтобы сверить ответ с соответствующим заданием работы.',
            uncertainty='Ожидается повторная проверка.',
        ).model_dump(mode='json')
    task.teacher = {
        **(task.teacher or {}), **changes, "confirmed_at": None, "final_status": None,
    }
    db.add(TeacherEdit(task_id=task.id, kind="correction", snapshot=task.teacher))
    schedule_submission(db, row)
    db.commit()
    return task_data(task)


@router.post("/{submission_id}/tasks/{task_id}/recheck", status_code=202, response_model=SubmissionView)
def recheck(request: Request, submission_id: UUID, task_id: UUID, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id, lock=True)
    check_idle(row)
    task = task_for(db, row, task_id)
    check_followups(db, user_id, sheets=request.app.state.settings.sheet_billing_enabled)
    from app.workspace import TeacherWorkspace
    workspace = db.get(TeacherWorkspace, user_id)
    data = workspace.data if workspace and isinstance(workspace.data, dict) else {}
    links = data.get('links') if isinstance(data.get('links'), dict) else {}
    link = links.get(row.id)
    test_id = link.get('testId') if isinstance(link, dict) else None
    snapshot = None
    if test_id:
        tests = data.get('tests') if isinstance(data.get('tests'), list) else []
        test = next((item for item in tests if isinstance(item, dict) and item.get('id') == test_id), None)
        if test is None:
            raise ApiError(409, 'test_not_found', 'Связанная работа не найдена. Проверьте раздел «Мои работы».')
        try:
            questions = test.get('questions')
            if not isinstance(questions, list) or not 1 <= len(questions) <= 50:
                raise ValueError()
            references = [TestQuestion.model_validate(item).model_dump() for item in questions]
            snapshot = json.dumps({'questions': references}, ensure_ascii=False)
            if len(snapshot) > 30000:
                raise ValueError()
        except (ValueError, TypeError):
            raise ApiError(409, 'invalid_test', 'Проверьте задания и ответы связанной работы.') from None
    job = enqueue(db, row, "recheck", task_id=task.id, clarification=snapshot)
    db.commit()
    notify(request.app.state, job.id, row.id, user_id)
    return submission_data(row)


@router.post("/{submission_id}/tasks/{task_id}/confirm", response_model=TaskView, response_model_exclude_unset=True)
def confirm(submission_id: UUID, task_id: UUID, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id, lock=True)
    check_idle(row)
    task = task_for(db, row, task_id)
    teacher = task.teacher or {}
    if not teacher.get("confirmed_at"):
        task.teacher = {
            **teacher, "confirmed_at": utcnow().isoformat(),
            "final_status": teacher.get("status") or task.preliminary["status"],
        }
        db.add(TeacherEdit(task_id=task.id, kind="confirmation", snapshot={
            **task.teacher, "recognition": task.recognition, "preliminary": task.preliminary,
        }))
        schedule_submission(db, row)
        db.commit()
    return task_data(task)


@router.post("/{submission_id}/confirm")
def confirm_work(submission_id: UUID, db: DB, user_id: Owner):
    row = owned(db, submission_id, user_id, lock=True)
    check_idle(row)
    tasks = db.scalars(select(DetectedTask).where(DetectedTask.submission_id == row.id)
                       .order_by(DetectedTask.position)).all()
    if not tasks or row.status != 'completed':
        raise ApiError(409, 'nothing_to_confirm', 'Нет готовых заданий для подтверждения.')
    for task in tasks:
        teacher = task.teacher or {}
        if not teacher.get('confirmed_at'):
            task.teacher = {**teacher, 'confirmed_at': utcnow().isoformat(),
                            'final_status': teacher.get('status') or task.preliminary['status']}
            db.add(TeacherEdit(task_id=task.id, kind='confirmation', snapshot={
                **task.teacher, 'recognition': task.recognition, 'preliminary': task.preliminary,
            }))
    schedule_submission(db, row)
    db.commit()
    return {'confirmed': len(tasks)}


@router.delete("/{submission_id}", status_code=204)
def delete_submission(request: Request, submission_id: UUID, db: DB, user_id: Owner):
    from app.work import clean_submission

    row = lock_submission(db, str(submission_id), user_id)
    if row is None or row.user_id != user_id:
        raise missing()
    if row.delete_requested_at is None:
        job = db.get(ProcessingJob, row.latest_job_id)
        if job and job.status in BUSY and job.reservation_id:
            settle_check(db, job.reservation_id, success=False)
        row.delete_requested_at = utcnow()
    db.commit()
    if not clean_submission(request.app.state, str(submission_id)):
        raise ApiError(503, "deletion_pending", "Работа скрыта. Удаление файлов будет повторено.")
    return Response(status_code=204)

import re
import json
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Thread
from time import monotonic

from sqlalchemy import and_, or_, select

from app.ai import (
    AIError, PageInput, PreliminaryReview, RecognizedTask, validate_recognition, validate_reviews,
)
from app.billing import settle_check
from app.core import as_utc, event, job_context, new_id, utcnow, worker_heartbeat
from app.submission_models import (
    DetectedTask, ProcessingJob, SourceFile, Submission, SubmissionPage, TaskAnalysis, TeacherEdit,
)
from app.submissions import BUSY, lock_submission


class LeaseLost(Exception):
    pass


class Analyses(list):
    def __init__(self, values, student_name=None):
        super().__init__(values)
        self.student_name = student_name


class StorageUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    job_id: str
    submission_id: str
    user_id: str
    request_id: str | None
    token: str
    attempt: int
    kind: str
    task_id: str | None


@dataclass(frozen=True)
class InputTask:
    task_id: str
    recognition: dict
    teacher: dict | None


@dataclass(frozen=True)
class Analysis:
    target_id: str | None
    recognition: RecognizedTask
    review_input: RecognizedTask
    review: PreliminaryReview


def _eligible(now):
    return or_(
        and_(ProcessingJob.status == "queued", ProcessingJob.next_attempt_at <= now),
        and_(ProcessingJob.status == "processing", ProcessingJob.lease_until <= now),
    )


def due_jobs(state, limit: int = 16) -> list[str]:
    with state.session_factory() as db:
        return list(db.scalars(
            select(ProcessingJob.id).join(Submission, Submission.latest_job_id == ProcessingJob.id)
            .where(Submission.delete_requested_at.is_(None), _eligible(utcnow()))
            .order_by(ProcessingJob.next_attempt_at, ProcessingJob.id).limit(limit)
        ))


def _claim(state, job_id: str) -> Claim | None:
    with state.session_factory.begin() as db:
        submission_id = db.scalar(select(ProcessingJob.submission_id).where(ProcessingJob.id == job_id))
        if submission_id is None:
            return None
        row = lock_submission(db, submission_id)
        if row is None or row.delete_requested_at is not None or row.latest_job_id != job_id:
            return None
        now = utcnow()
        job = db.scalar(select(ProcessingJob).where(ProcessingJob.id == job_id, _eligible(now)))
        if job is None:
            return None
        if job.attempts >= state.settings.max_attempts:
            _fail_final(db, row, job, "worker_lease_expired")
            event("job_failed", component="worker", level="ERROR", job_id=job.id,
                  submission_id=row.id, user_id=row.user_id, error_code="worker_lease_expired")
            return None
        job.attempts += 1
        job.status = row.status = "processing"
        job.stage = row.stage = "preparing_pages" if job.kind != "recheck" else "preparing_review"
        job.error_code = row.error_code = None
        job.lease_token = new_id()
        job.lease_until = now + timedelta(seconds=state.settings.job_lease_seconds)
        return Claim(job.id, row.id, row.user_id, job.origin_request_id, job.lease_token,
                     job.attempts, job.kind, job.task_id)


def _active_job(db, claim: Claim) -> tuple[Submission, ProcessingJob]:
    row = lock_submission(db, claim.submission_id)
    if row is None or row.delete_requested_at is not None or row.latest_job_id != claim.job_id:
        raise LeaseLost()
    job = db.get(ProcessingJob, claim.job_id)
    if (job is None or job.status != "processing" or job.lease_token != claim.token
            or as_utc(job.lease_until) <= utcnow()):
        raise LeaseLost()
    return row, job


def _heartbeat(state, claim: Claim, stopped: Event) -> None:
    interval = state.settings.job_lease_seconds / 3
    while not stopped.wait(interval):
        try:
            with state.session_factory.begin() as db:
                _, job = _active_job(db, claim)
                job.lease_until = utcnow() + timedelta(seconds=state.settings.job_lease_seconds)
            worker_heartbeat(state.settings.worker_heartbeat_path)
        except LeaseLost:
            return
        except Exception:
            event("job_heartbeat_failed", component="worker", level="WARNING", job_id=claim.job_id,
                  submission_id=claim.submission_id, error_code="database_unavailable")


def _stage(state, claim: Claim, name: str) -> None:
    with state.session_factory.begin() as db:
        row, job = _active_job(db, claim)
        previous = job.stage
        job.stage = row.stage = name
    event("job_stage_completed", stage=previous)
    event("job_stage_started", stage=name)


def _load_inputs(state, claim: Claim) -> tuple[list, list[InputTask], str | None, list, str | None, str | None]:
    with state.session_factory() as db:
        pages = db.execute(
            select(SubmissionPage.number, SubmissionPage.object_key)
            .where(SubmissionPage.submission_id == claim.submission_id).order_by(SubmissionPage.number)
        ).all() if claim.kind != "recheck" else []
        query = select(DetectedTask).where(DetectedTask.submission_id == claim.submission_id)
        if claim.task_id:
            query = query.where(DetectedTask.id == claim.task_id)
        tasks = [InputTask(task.id, task.recognition, task.teacher) for task in db.scalars(query)]
        notes = list(db.scalars(
            select(ProcessingJob.clarification).where(
                ProcessingJob.submission_id == claim.submission_id,
                ProcessingJob.kind == "clarification",
                ProcessingJob.clarification.is_not(None),
            ).order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc()).limit(4)
        ))
        initial = db.scalar(select(ProcessingJob.clarification).where(
            ProcessingJob.submission_id == claim.submission_id, ProcessingJob.kind == "initial"
        ).order_by(ProcessingJob.created_at.asc()).limit(1))
    clarification = "\n\n".join(reversed(notes))[-4000:] if notes else None
    try:
        metadata = json.loads(initial) if initial else []
        subject = metadata.get('subject') if isinstance(metadata, dict) else None
        subject_category = metadata.get('subject_category') if isinstance(metadata, dict) else None
        references = metadata.get('questions', []) if isinstance(metadata, dict) else metadata
        if not isinstance(references, list):
            references = []
    except (ValueError, TypeError):
        references = []
        subject = None
        subject_category = None
    return pages, tasks, clarification, references, subject, subject_category


def _source_pages(state, pages: list) -> list[PageInput]:
    result = []
    for number, key in pages:
        try:
            content, mime_type = state.storage.get(key)
        except Exception as error:
            raise StorageUnavailable() from error
        result.append(PageInput(number=number, content=content, mime_type=mime_type))
    return result


def _effective(recognition: RecognizedTask, teacher: dict | None) -> RecognizedTask:
    data = recognition.model_dump()
    if teacher and "condition" in teacher:
        corrected_condition = (teacher["condition"] or "").strip() or None
        if corrected_condition != recognition.condition:
            data["condition"] = corrected_condition
            data["kind"] = "unknown"
            data["ambiguity"] = None
    if teacher and "answer" in teacher:
        data["answer"] = teacher["answer"] or None
        data["answer_state"] = "present" if data["answer"] else "unknown"
    return RecognizedTask.model_validate(data)


def _match_keys(task: RecognizedTask) -> list[tuple]:
    keys = []
    if task.number:
        keys.append(("number", task.page_number, task.number))
    if task.answer or task.condition:
        keys.append(("text", task.page_number, task.condition, task.answer))
    if not task.number and task.answer:
        keys.append(("answer", task.page_number, task.answer))
    return keys


def _match_tasks(recognition: list[RecognizedTask], previous: list[InputTask]) -> list[InputTask | None]:
    old_entries = [(key, item) for item in previous
                   for key in _match_keys(RecognizedTask.model_validate(item.recognition))]
    old_counts = Counter(key for key, _ in old_entries)
    old = {key: item for key, item in old_entries if old_counts[key] == 1}
    new_counts = Counter(key for task in recognition for key in _match_keys(task))
    seen: set[str] = set()
    matched = []
    for task in recognition:
        match = next(
            (old[key] for key in _match_keys(task)
             if new_counts[key] == 1 and key in old and old[key].task_id not in seen),
            None,
        )
        if match:
            seen.add(match.task_id)
        matched.append(match)
    return matched


def _analyze(state, claim: Claim) -> list[Analysis]:
    pages, existing, clarification, references, subject, subject_category = _load_inputs(state, claim)
    provider = state.ai_provider
    student_name = None
    if claim.kind == "recheck":
        if len(existing) != 1:
            raise LeaseLost()
        recognition = [RecognizedTask.model_validate(existing[0].recognition)]
        matched = existing
    else:
        source = _source_pages(state, pages)
        _stage(state, claim, "recognition")
        if hasattr(provider, "recognize_with_name"):
            found, student_name = provider.recognize_with_name(source, clarification, subject=subject, subject_category=subject_category)
        else:
            found = provider.recognize(source, clarification)
        recognition = validate_recognition(found, source)
        matched = _match_tasks(recognition, existing)

    _stage(state, claim, "preliminary_review")
    review_inputs = [
        _effective(task, match.teacher if match else None)
        for task, match in zip(recognition, matched, strict=True)
    ]
    if not references:
        raw_reviews = provider.review(review_inputs, clarification) if review_inputs else []
        reviews = [review.model_copy(update={"expected_answer_source": None}) for review in
                   validate_reviews(review_inputs, raw_reviews, clarification)]
    else:
        reviews = []
        numbers = Counter(task.number for task in review_inputs if task.number)
        for task in review_inputs:
            if not task.number or not task.number.isdecimal() or numbers[task.number] != 1:
                reviews.append(PreliminaryReview(status="needs_review", explanation="Не удалось однозначно сопоставить задание с выбранной работой.", uncertainty="Проверьте номер задания."))
                continue
            number = int(task.number)
            if not 1 <= number <= len(references):
                reviews.append(PreliminaryReview(status="needs_review", explanation="Для этого номера нет задания в выбранной работе.", uncertainty="Проверьте номер задания."))
                continue
            reference = references[number - 1]
            if not isinstance(reference, dict) or not reference.get('question') or not reference.get('answer'):
                reviews.append(PreliminaryReview(status="needs_review", explanation="Эталон задания недоступен.", uncertainty="Проверьте работу."))
                continue
            expected = reference['answer']
            if task.ambiguity or task.answer_state != 'present' or not task.answer:
                review = PreliminaryReview(status="needs_review", explanation="Ответ не удалось однозначно распознать.", uncertainty="Сверьте ответ с фотографией.")
            elif normalize_answer(task.answer) == normalize_answer(expected):
                review = PreliminaryReview(status="correct", explanation="Распознанный ответ совпадает с ответом учителя в работе.")
            elif hasattr(provider, 'review_reference'):
                review = provider.review_reference(task, reference, clarification)
            else:
                review = PreliminaryReview(status="needs_review", explanation="Ответ отличается от ответа учителя.", uncertainty="Сверьте ответ вручную.")
            reviews.append(review.model_copy(update={"expected_answer": expected, "expected_answer_source": "teacher_test"}))
    _stage(state, claim, "validation")
    return Analyses([
        Analysis(match.task_id if match else None, task, review_input, review)
        for match, task, review_input, review in zip(matched, recognition, review_inputs, reviews, strict=True)
    ], student_name)


def normalize_answer(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _persist(state, claim: Claim, analyses: list[Analysis]) -> None:
    provider = state.ai_provider
    with state.session_factory.begin() as db:
        row, job = _active_job(db, claim)
        if not row.student_reference and getattr(analyses, "student_name", None):
            row.student_reference = analyses.student_name
        existing = db.scalars(select(DetectedTask).where(DetectedTask.submission_id == row.id)).all()
        position = max((task.position for task in existing), default=0)
        by_id = {task.id: task for task in existing}
        for item in analyses:
            recognized = item.recognition.model_dump(mode="json")
            reviewed = item.review.model_dump(mode="json")
            task = by_id.get(item.target_id)
            if task is None:
                position += 1
                task = DetectedTask(
                    id=new_id(), submission_id=row.id, position=position,
                    page_number=item.recognition.page_number,
                    original_recognition=recognized, original_preliminary=reviewed,
                    recognition=recognized, preliminary=reviewed, teacher=None,
                    provider=provider.name, demonstration=provider.demonstration,
                )
                db.add(task)
                db.flush()
            else:
                changed = task.recognition != recognized or task.preliminary != reviewed
                task.recognition, task.preliminary = recognized, reviewed
                task.page_number = item.recognition.page_number
                task.provider = provider.name
                task.demonstration = task.demonstration or provider.demonstration
                if task.teacher and task.teacher.get("confirmed_at") and (changed or claim.kind == "recheck"):
                    task.teacher = {**task.teacher, "confirmed_at": None, "final_status": None}
                    db.add(TeacherEdit(task_id=task.id, kind="review_updated", snapshot=task.teacher))
            db.add(TaskAnalysis(
                task_id=task.id, job_id=job.id, recognition=recognized,
                review_input=item.review_input.model_dump(mode="json"), preliminary=reviewed,
            ))

        if job.reservation_id:
            settle_check(db, job.reservation_id, success=True)
        now = utcnow()
        row.first_completed_at = row.first_completed_at or now
        row.status = job.status = "completed"
        row.stage = job.stage = "completed"
        job.provider, job.demonstration = provider.name, provider.demonstration
        job.finished_at = now
        job.lease_token = job.lease_until = None


def _fail_final(db, row: Submission, job: ProcessingJob, code: str) -> None:
    if job.reservation_id:
        settle_check(db, job.reservation_id, success=False)
    row.status = job.status = "failed"
    row.error_code = job.error_code = code
    job.finished_at = utcnow()
    job.lease_token = job.lease_until = None


def _fail(state, claim: Claim, code: str, retryable: bool) -> None:
    with state.session_factory.begin() as db:
        row, job = _active_job(db, claim)
        stage = job.stage
        retry = retryable and job.attempts < state.settings.max_attempts
        if retry:
            row.status = job.status = "queued"
            row.error_code = job.error_code = code
            delay = state.settings.retry_delay_seconds * 2 ** (job.attempts - 1)
            job.next_attempt_at = utcnow() + timedelta(seconds=delay)
            job.lease_token = job.lease_until = None
        else:
            _fail_final(db, row, job, code)
    event("job_retry_scheduled" if retry else "job_failed", stage=stage, error_code=code,
          level="WARNING" if retry else "ERROR")


def run_job(state, job_id: str) -> None:
    claim = _claim(state, job_id)
    if claim is None:
        return
    token = job_context.set({
        "component": "worker", "job_id": claim.job_id, "submission_id": claim.submission_id,
        "user_id": claim.user_id, "request_id": claim.request_id, "attempt": claim.attempt,
    })
    start = monotonic()
    stopped = Event()
    thread = Thread(target=_heartbeat, args=(state, claim, stopped), daemon=True)
    thread.start()
    try:
        event("job_started", kind=claim.kind)
        try:
            analyses = _analyze(state, claim)
            _stage(state, claim, "saving_results")
            _persist(state, claim, analyses)
        except AIError as error:
            code = error.code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error.code) else "ai_error"
            _fail(state, claim, code, error.retryable)
        except StorageUnavailable:
            _fail(state, claim, "storage_unavailable", True)
        except LeaseLost:
            raise
        except Exception:
            _fail(state, claim, "processing_error", True)
        else:
            event("job_stage_completed", stage="saving_results")
            event("job_completed", duration_ms=round((monotonic() - start) * 1000))
    except LeaseLost:
        event("job_lease_lost", level="WARNING")
    finally:
        stopped.set()
        thread.join()
        job_context.reset(token)


def clean_submission(state, submission_id: str) -> bool:
    try:
        with state.session_factory.begin() as db:
            row = lock_submission(db, submission_id)
            if row is None:
                return True
            if row.delete_requested_at is None:
                return False
            user_id = row.user_id
            for model in (SubmissionPage, SourceFile):
                keys = db.scalars(select(model.object_key).where(model.submission_id == row.id))
                for key in keys:
                    state.storage.delete(key)
            db.delete(row)
    except Exception:
        event("submission_cleanup_failed", level="ERROR", submission_id=submission_id,
              error_code="cleanup_unavailable")
        return False
    event("submission_deleted", submission_id=submission_id, user_id=user_id)
    return True


def cleanup_due(state, limit: int = 8) -> None:
    with state.session_factory() as db:
        ids = list(db.scalars(
            select(Submission.id).where(Submission.delete_requested_at.is_not(None))
            .order_by(Submission.delete_requested_at).limit(limit)
        ))
    for submission_id in ids:
        clean_submission(state, submission_id)

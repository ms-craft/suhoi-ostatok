from pydantic import ValidationError

from .contracts import (
    AIError,
    AnswerState,
    PreliminaryReview,
    RecognizedTask,
    ReviewStatus,
    TaskKind,
)
from .formal import formal_check


def _needs_review(
    reason: str, missing_data: list[str] | None = None, *, explanation: str | None = None
) -> PreliminaryReview:
    return PreliminaryReview(
        status=ReviewStatus.NEEDS_REVIEW,
        explanation=explanation or reason,
        uncertainty=reason,
        missing_data=missing_data or [],
    )


def local_review(task: RecognizedTask) -> PreliminaryReview | None:
    """Return a result when a backend rule fully determines the disposition."""
    if task.answer_state == AnswerState.UNREADABLE:
        return PreliminaryReview(
            status=ReviewStatus.UNREADABLE,
            explanation="Ответ не удалось разборчиво прочитать.",
            uncertainty="Запись частично или полностью неразборчива.",
            missing_data=["Более чёткое изображение или расшифровка ответа"],
        )
    if not task.condition:
        return _needs_review(
            "Условие не найдено. Правильность ответа определить невозможно.",
            ["Условие задания"],
        )
    if task.ambiguity:
        return _needs_review(task.ambiguity, ["Однозначное условие или критерии"])
    if task.answer_state == AnswerState.BLANK:
        return PreliminaryReview(
            status=ReviewStatus.NO_ANSWER, explanation="Область для ответа явно оставлена пустой."
        )
    if task.answer_state != AnswerState.PRESENT:
        return _needs_review(
            "По странице нельзя установить, отсутствует ли ответ или он не распознан.",
            ["Подтверждение или расшифровка ответа"],
        )

    result = formal_check(task)
    if result is not None:
        matches, expected = result
        return PreliminaryReview(
            status=ReviewStatus.CORRECT if matches else ReviewStatus.INCORRECT,
            explanation=(
                "Результат формальной проверки совпадает с ответом."
                if matches else "Результат формальной проверки отличается от ответа."
            ),
            expected_answer=expected,
        )
    return None


def _verified_review(task: RecognizedTask, review: PreliminaryReview) -> PreliminaryReview:
    verified = local_review(task)
    if verified is not None:
        return verified
    if task.kind == TaskKind.OPEN:
        return _needs_review(
            review.uncertainty or "Развёрнутый ответ требует проверки учителем.",
            review.missing_data,
            explanation=review.explanation if review.status == ReviewStatus.NEEDS_REVIEW else None,
        )
    if task.kind == TaskKind.UNKNOWN:
        return _needs_review(
            review.uncertainty or "Не удалось однозначно определить вид задания.",
            review.missing_data,
            explanation=review.explanation if review.status == ReviewStatus.NEEDS_REVIEW else None,
        )
    if review.status in {ReviewStatus.NO_ANSWER, ReviewStatus.UNREADABLE}:
        return _needs_review("Предварительная оценка противоречит распознанному ответу.")
    if review.uncertainty or review.missing_data or review.status == ReviewStatus.NEEDS_REVIEW:
        return _needs_review(
            review.uncertainty or "Недостаточно данных для однозначной предварительной оценки.",
            review.missing_data,
            explanation=review.explanation if review.status == ReviewStatus.NEEDS_REVIEW else None,
        )
    return review


def validate_reviews(
    tasks: list[RecognizedTask], reviews: list[PreliminaryReview], clarification: str | None = None
) -> list[PreliminaryReview]:
    # Free-form clarification cannot on its own prove which exercise a missing condition belongs to.
    # Recognition must first associate the supplied evidence with a concrete task.
    if len(tasks) != len(reviews):
        raise AIError("ai_invalid_review_count")
    try:
        return [
            _verified_review(task, PreliminaryReview.model_validate(review.model_dump()))
            for task, review in zip(tasks, reviews, strict=True)
        ]
    except (AttributeError, ValidationError):
        raise AIError("ai_invalid_review") from None


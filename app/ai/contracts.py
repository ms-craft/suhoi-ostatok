from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class AIError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AnswerState(StrEnum):
    PRESENT = "present"
    BLANK = "blank"
    UNREADABLE = "unreadable"
    UNKNOWN = "unknown"


class TaskKind(StrEnum):
    ARITHMETIC = "arithmetic"
    NUMERIC_COMPARISON = "numeric_comparison"
    MEASUREMENT = "measurement"
    CLOSED = "closed"
    OPEN = "open"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    PARTIALLY_CORRECT = "partially_correct"
    NEEDS_REVIEW = "needs_review"
    NO_ANSWER = "no_answer"
    UNREADABLE = "unreadable"


class AIRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)


class AnswerRegion(AIRecord):
    """Bounding box normalized to the displayed page, measured from the top left."""

    x_min: float = Field(ge=0, lt=1)
    y_min: float = Field(ge=0, lt=1)
    x_max: float = Field(gt=0, le=1)
    y_max: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def ordered_edges(self) -> "AnswerRegion":
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("The answer region must have positive width and height")
        return self


ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class RecognizedTask(AIRecord):
    page_number: int = Field(ge=1, strict=True)
    number: str | None = Field(default=None, min_length=1, max_length=80)
    condition: str | None = Field(default=None, min_length=1, max_length=8000)
    answer: str | None = Field(default=None, min_length=1, max_length=16000)
    answer_state: AnswerState = AnswerState.UNKNOWN
    kind: TaskKind = TaskKind.UNKNOWN
    region: AnswerRegion | None = None
    ambiguity: ShortText | None = None
    corrections: list[ShortText] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def consistent_answer(self) -> "RecognizedTask":
        if self.answer_state == AnswerState.PRESENT and self.answer is None:
            raise ValueError("A present answer requires transcribed text")
        if self.answer_state == AnswerState.BLANK and self.answer is not None:
            raise ValueError("A blank answer cannot contain transcribed text")
        return self


class PreliminaryReview(AIRecord):
    status: ReviewStatus
    explanation: str = Field(min_length=1, max_length=4000)
    expected_answer: str | None = Field(default=None, min_length=1, max_length=3000)
    expected_answer_source: str | None = None
    uncertainty: ShortText | None = None
    missing_data: list[ShortText] = Field(default_factory=list, max_length=12)


@dataclass(frozen=True, slots=True)
class PageInput:
    number: int
    content: bytes
    mime_type: str


class AIProvider(Protocol):
    name: str
    demonstration: bool

    def recognize(
        self, pages: list[PageInput], clarification: str | None
    ) -> list[RecognizedTask]: ...

    def review(
        self, tasks: list[RecognizedTask], clarification: str | None
    ) -> list[PreliminaryReview]: ...


def validate_recognition(
    tasks: list[RecognizedTask], pages: list[PageInput]
) -> list[RecognizedTask]:
    page_numbers = {page.number for page in pages}
    if len(tasks) > min(500, 50 * len(pages)):
        raise AIError("ai_invalid_recognition")
    validated = []
    try:
        for task in tasks:
            task = RecognizedTask.model_validate(task.model_dump())
            if task.page_number not in page_numbers:
                raise AIError("ai_invalid_source_page")
            validated.append(task)
    except (AttributeError, ValidationError):
        raise AIError("ai_invalid_recognition") from None
    return validated

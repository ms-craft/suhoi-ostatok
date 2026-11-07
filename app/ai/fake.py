from .contracts import PageInput, PreliminaryReview, RecognizedTask, ReviewStatus, validate_recognition
from .review import validate_reviews


class FakeAIProvider:
    """A predictable demo with fictional tasks, independent of image contents."""

    name = "fake"
    demonstration = True

    def __init__(self, fixtures: list[RecognizedTask] | None = None):
        self.fixtures = fixtures

    def extract_questions(self, page: PageInput) -> list[str]:
        return ['Демонстрационное задание: вычислите 2 + 2.']

    def recognize(
        self, pages: list[PageInput], clarification: str | None
    ) -> list[RecognizedTask]:
        tasks = self.fixtures
        if tasks is None:
            tasks = [
                RecognizedTask(
                    page_number=page.number,
                    number="Демо",
                    condition="2 + 2",
                    answer="5",
                    answer_state="present",
                    kind="arithmetic",
                )
                for page in pages
            ]
        return validate_recognition(tasks, pages)

    def review(
        self, tasks: list[RecognizedTask], clarification: str | None
    ) -> list[PreliminaryReview]:
        placeholder = PreliminaryReview(
            status=ReviewStatus.NEEDS_REVIEW,
            explanation="Демонстрационный результат. Изображение не анализировалось.",
        )
        return validate_reviews(tasks, [placeholder] * len(tasks), clarification)

"""Run a fixed synthetic evaluation. No user files are accepted."""

import argparse
import json
import os
import unicodedata
from collections import Counter
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.ai import AIError, GeminiAIProvider, PageInput, PreliminaryReview, RecognizedTask, validate_reviews


@dataclass(frozen=True)
class Example:
    name: str
    pages: int
    gold: tuple[tuple[RecognizedTask, str], ...]
    degraded: bool = False


def exercise(page, number, condition, answer, kind, status, answer_state="present"):
    return RecognizedTask(
        page_number=page, number=number, condition=condition, answer=answer,
        kind=kind, answer_state=answer_state,
    ), status


EXAMPLES = (
    Example("two-pages", 2, (
        exercise(1, "1", "Calculate: 12 + 8", "20", "arithmetic", "correct"),
        exercise(1, "2", "Calculate: 5 * 4", "19", "arithmetic", "incorrect"),
        exercise(2, "3", "Compare: 4.5 ? 4.05", ">", "numeric_comparison", "correct"),
        exercise(2, "4", "Convert: 1 m to cm", "100 cm", "measurement", "correct"),
    )),
    Example("open-and-unanswered", 1, (
        exercise(1, "1", "Explain why leaves change color.", "Leaves get less sunlight.",
                 "open", "needs_review"),
        exercise(1, "2", "Calculate: 9 + 7", None, "arithmetic", "no_answer", "blank"),
    )),
    Example("rotated-scan", 1, (
        exercise(1, "1", "Calculate: 17 - 9", "7", "arithmetic", "incorrect"),
    ), degraded=True),
    Example("blank-page", 1, ()),
)


def render(example: Example) -> list[PageInput]:
    pages = []
    font = ImageFont.load_default(size=32)
    for number in range(1, example.pages + 1):
        image = Image.new("RGB", (1300, 950), "white")
        draw = ImageDraw.Draw(image)
        tasks = [task for task, _ in example.gold if task.page_number == number]
        for index, task in enumerate(tasks):
            top = 90 + index * 220
            draw.text((75, top), f"{task.number}. {task.condition}", fill="black", font=font)
            draw.text((105, top + 68), "Answer:", fill="black", font=font)
            draw.rectangle((240, top + 60, 1190, top + 119), outline="black", width=2)
            if task.answer is not None:
                draw.text((260, top + 69), task.answer, fill="#21346b", font=font)
        if example.degraded:
            image = image.rotate(3, resample=Image.Resampling.BICUBIC, fillcolor="white")
            image = image.filter(ImageFilter.GaussianBlur(0.8))
        with BytesIO() as buffer:
            image.save(buffer, format="JPEG", quality=82)
            pages.append(PageInput(number, buffer.getvalue(), "image/jpeg"))
    return pages


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _key(task: RecognizedTask) -> tuple[int, str | None]:
    return task.page_number, _normalized(task.number)


def compare(example, recognized, reviews) -> Counter:
    assert len(recognized) == len(reviews)
    observed = {}
    duplicate = set()
    for task, review in zip(recognized, reviews, strict=True):
        key = _key(task)
        if key in observed:
            duplicate.add(key)
        observed[key] = task, review
    expected = {_key(task): (task, status) for task, status in example.gold}
    matched = (expected.keys() & observed.keys()) - duplicate
    metrics = Counter(
        expected=len(expected), detected=len(recognized), matched=len(matched),
        missing=len(expected) - len(matched), extra=len(recognized) - len(matched),
        condition_exact=0, answer_exact=0, status_exact=0,
    )
    for key in matched:
        gold, status = expected[key]
        task, review = observed[key]
        metrics["condition_exact"] += _normalized(task.condition) == _normalized(gold.condition)
        metrics["answer_exact"] += (
            _normalized(task.answer) == _normalized(gold.answer)
            and task.answer_state == gold.answer_state
        )
        metrics["status_exact"] += review.status == status
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("offline", "gemini"), default="offline")
    parser.add_argument("--case", choices=[example.name for example in EXAMPLES])
    args = parser.parse_args()
    provider = None
    if args.provider == "gemini":
        settings = None
        try:
            from app.config import get_settings
            settings = get_settings()
        except Exception:
            pass
        key = os.getenv("GEMINI_API_KEY") or (
            settings.gemini_api_key.get_secret_value() if settings and settings.gemini_api_key else ""
        )
        key = (key or "").strip()
        if not key:
            parser.error("GEMINI_API_KEY is required for an explicit live synthetic run")
        try:
            model = os.getenv("GEMINI_MODEL") or (settings.gemini_model if settings else "gemini-3.8-flash")
            api_base = os.getenv("GEMINI_API_BASE") or (settings.gemini_api_base if settings else None)
            kwargs = {"api_base": api_base} if api_base else {}
            provider = GeminiAIProvider(key, model=model, **kwargs)
        except AIError as error:
            parser.error(error.code)

    totals = Counter()
    errors = 0
    for example in EXAMPLES:
        if args.case and example.name != args.case:
            continue
        try:
            if provider:
                recognized = provider.recognize(render(example), None)
                reviews = provider.review(recognized, None)
            else:
                recognized = [task for task, _ in example.gold]
                placeholder = PreliminaryReview(status="needs_review", explanation="Offline fixture")
                reviews = validate_reviews(recognized, [placeholder] * len(recognized))
            metrics = compare(example, recognized, reviews)
        except AIError as error:
            print(json.dumps({"case": example.name, "error": error.code}))
            errors += 1
            continue
        totals.update(metrics)
        if provider is None:
            del metrics["condition_exact"], metrics["answer_exact"]
        print(json.dumps({"case": example.name, **metrics}))
    if provider is None:
        del totals["condition_exact"], totals["answer_exact"]
    print(json.dumps({
        "mode": "live_synthetic" if provider else "offline_teacher_transcriptions",
        "model": provider.model if provider else None,
        "errors": errors,
        "totals": dict(totals),
    }))
    return int(bool(errors or totals["missing"] or totals["extra"]
                    or totals["status_exact"] != totals["expected"]
                    or (provider and (totals["condition_exact"] != totals["expected"]
                                      or totals["answer_exact"] != totals["expected"]))))


if __name__ == "__main__":
    raise SystemExit(main())

"""Клиент локального анонимизатора «Сухого остатка».

Перед отправкой страниц в Gemini изображения проходят локальный сайдкар
(PP-OCR + PARSeq): ФИО и подпись закрашиваются, а распознанное имя
возвращается в приложение. Оригиналы страниц остаются в S3 и доступны
учителю; в Gemini уходит только обезличенная копия.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from app.ai import PageInput
from app.config import Settings
from app.core import event


class PrivacyUnavailable(Exception):
    """Локальный анонимизатор недоступен или вернул ошибку."""


@dataclass(frozen=True)
class Redaction:
    content: bytes
    mime_type: str
    name: str | None
    meta: dict


class PrivacyClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.mode = settings.privacy_mode
        self.url = settings.privacy_url.rstrip("/")
        self.token = (
            settings.privacy_token.get_secret_value()
            if settings.privacy_token is not None else None
        )
        self._client = client or httpx.Client(timeout=settings.privacy_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def _headers(self) -> dict[str, str]:
        return {"X-Privacy-Token": self.token} if self.token else {}

    def health(self) -> bool:
        if not self.enabled:
            return True
        try:
            response = self._client.get(f"{self.url}/health", headers=self._headers())
            return response.status_code == 200 and bool(response.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    def redact_page(self, content: bytes, mime_type: str) -> Redaction:
        if not self.enabled:
            return Redaction(content, mime_type, None, {})
        payload = {
            "image": base64.b64encode(content).decode("ascii"),
            "mime": mime_type,
        }
        try:
            response = self._client.post(
                f"{self.url}/redact", json=payload, headers=self._headers(),
            )
        except httpx.HTTPError as error:
            raise PrivacyUnavailable(str(error)) from error
        if response.status_code != 200:
            raise PrivacyUnavailable(f"privacy HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as error:
            raise PrivacyUnavailable("privacy invalid response") from error
        if not data.get("ok") or not data.get("image"):
            raise PrivacyUnavailable(str(data.get("error") or "privacy empty response"))
        try:
            redacted = base64.b64decode(data["image"], validate=False)
        except (ValueError, TypeError) as error:
            raise PrivacyUnavailable("privacy invalid image") from error
        return Redaction(
            content=redacted,
            mime_type=str(data.get("mime") or "image/jpeg"),
            name=(str(data["name"]).strip() or None) if data.get("name") else None,
            meta=data,
        )

    def redact_pages(self, pages: list[PageInput]) -> tuple[list[PageInput], str | None]:
        if not self.enabled:
            return pages, None
        result: list[PageInput] = []
        names: list[str] = []
        for page in pages:
            try:
                redaction = self.redact_page(page.content, page.mime_type)
            except PrivacyUnavailable as error:
                if self.mode == "require":
                    raise
                event(
                    "privacy_unavailable", level="WARNING",
                    error_code="privacy_unavailable", page=page.number,
                )
                result.append(page)
                continue
            result.append(PageInput(
                number=page.number,
                content=redaction.content,
                mime_type=redaction.mime_type,
            ))
            if redaction.name:
                names.append(redaction.name)
            event(
                "privacy_redacted", page=page.number,
                name_found=bool(redaction.name),
                student_name=redaction.name,
                redactions=(redaction.meta.get("counts") or {}),
            )
        # Для многолистовой работы имя обычно на первом листе. Если на
        # разных страницах прочитались разные имена, берём первое, а не
        # теряем имя совсем.
        if len({name.casefold() for name in names}) > 1:
            event("privacy_multiple_names", level="WARNING", pages=len(pages))
        return result, names[0] if names else None

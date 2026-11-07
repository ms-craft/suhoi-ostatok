import base64

import httpx
import pytest

from app.ai import PageInput
from app.config import Settings
from app.privacy import PrivacyClient, PrivacyUnavailable


def _settings(**overrides):
    values = {
        "ai_provider": "fake",
        "privacy_mode": "require",
        "privacy_url": "http://privacy.test",
    }
    values.update(overrides)
    return Settings(**values)


def _client(handler, **overrides):
    transport = httpx.MockTransport(handler)
    return PrivacyClient(
        _settings(**overrides),
        client=httpx.Client(transport=transport),
    )


def test_redacts_page_and_returns_name():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/redact"
        payload = request.read()
        assert b"image" in payload
        return httpx.Response(200, json={
            "ok": True,
            "image": base64.b64encode(b"redacted-image").decode("ascii"),
            "mime": "image/jpeg",
            "name": "Иванов Иван",
            "counts": {"name": 1, "header": 0, "signature": 1},
        })

    client = _client(handler)
    pages, name = client.redact_pages([
        PageInput(number=1, content=b"original", mime_type="image/jpeg"),
    ])
    assert name == "Иванов Иван"
    assert pages[0].content == b"redacted-image"
    assert pages[0].mime_type == "image/jpeg"


def test_multipage_work_keeps_first_page_name():
    names = iter(["Иванов Иван", "Петров Пётр"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True,
            "image": base64.b64encode(b"redacted").decode("ascii"),
            "mime": "image/jpeg",
            "name": next(names),
            "counts": {"name": 1},
        })

    client = _client(handler)
    pages, name = client.redact_pages([
        PageInput(number=1, content=b"one", mime_type="image/jpeg"),
        PageInput(number=2, content=b"two", mime_type="image/jpeg"),
    ])
    assert name == "Иванов Иван"
    assert len(pages) == 2


def test_auto_mode_falls_back_to_original_when_sidecar_is_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar down", request=request)

    client = _client(handler, privacy_mode="auto")
    pages, name = client.redact_pages([
        PageInput(number=1, content=b"original", mime_type="image/jpeg"),
    ])
    assert name is None
    assert pages[0].content == b"original"


def test_require_mode_raises_when_sidecar_is_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar down", request=request)

    client = _client(handler, privacy_mode="require")
    with pytest.raises(PrivacyUnavailable):
        client.redact_pages([
            PageInput(number=1, content=b"original", mime_type="image/jpeg"),
        ])


def test_off_mode_does_not_call_sidecar():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("sidecar must not be called")

    client = _client(handler, privacy_mode="off")
    pages, name = client.redact_pages([
        PageInput(number=1, content=b"original", mime_type="image/jpeg"),
    ])
    assert name is None
    assert pages[0].content == b"original"


def test_health_checks_sidecar():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"ok": True})

    assert _client(handler).health() is True

from time import monotonic

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import accounts, billing, submissions
from app.ai import AIProvider, FakeAIProvider, GeminiAIProvider
from app.config import Settings, get_settings
from app.core import ApiError, configure_logging, event, new_id, request_id
from app.db import make_session_factory
from app.queue import JobQueue, make_queue
from app.storage import Storage, make_storage


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


def error_response(status: int, code: str, message: str, headers=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id.get()}},
        headers=headers,
    )


class RequestBoundary:
    def __init__(self, app: ASGIApp, max_body: int):
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identifier = new_id()
        token = request_id.set(identifier)
        started = monotonic()
        status = 500
        response_started = False
        received = 0
        oversized = False

        async def count_body() -> Message:
            nonlocal received, oversized
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    oversized = True
                    # Starlette closes multipart spool files when this exception is raised.
                    raise MultiPartException("request_too_large")
            return message

        async def add_request_id(message: Message) -> None:
            nonlocal status, response_started
            if oversized:
                if message["type"] == "http.response.start" and not response_started:
                    response = error_response(413, "request_too_large", "Слишком большой запрос.")
                    await response(scope, receive, tracked_send)
                return
            await tracked_send(message)

        async def tracked_send(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = [*message.get("headers", []), (b"x-request-id", identifier.encode("ascii"))]
                if scope["path"].startswith("/v1/") and not any(key == b"cache-control" for key, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            for key, value in scope["headers"]:
                if key == b"content-length" and (not value.isdigit() or len(value) > 10):
                    await error_response(400, "invalid_request", "Некорректный запрос.")(
                        scope, receive, tracked_send,
                    )
                    return
                if key == b"content-length" and int(value) > self.max_body:
                    await error_response(413, "request_too_large", "Слишком большой запрос.")(
                        scope, receive, tracked_send,
                    )
                    return
            await self.app(scope, count_body, add_request_id)
        except Exception:
            if oversized and not response_started:
                await error_response(413, "request_too_large", "Слишком большой запрос.")(
                    scope, receive, tracked_send,
                )
            else:
                event("request_failed", level="ERROR", error_code="internal_error")
                if not response_started:
                    await error_response(500, "internal_error", "Внутренняя ошибка.")(
                        scope, receive, tracked_send,
                    )
        finally:
            route = getattr(scope.get("route"), "path", None)
            event("http_request", method=scope["method"], route=route, status=status,
                  duration_ms=round((monotonic() - started) * 1000))
            request_id.reset(token)


def create_app(
    settings: Settings | None = None, *, storage: Storage | None = None,
    queue: JobQueue | None = None, ai_provider: AIProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    local_docs = settings.deployment_mode == "local"
    app = FastAPI(
        title="Сухой остаток",
        version="0.1.0",
        docs_url="/docs" if local_docs else None,
        redoc_url="/redoc" if local_docs else None,
        openapi_url="/openapi.json" if local_docs else None,
    )
    app.state.settings = settings
    app.state.session_factory = make_session_factory(settings.database_url)
    app.state.storage = storage if storage is not None else make_storage(settings)
    app.state.queue = queue if queue is not None else make_queue(settings)
    if ai_provider is None:
        if settings.ai_provider == "gemini":
            ai_provider = GeminiAIProvider(
                settings.gemini_api_key.get_secret_value(),
                settings.gemini_model,
                api_base=settings.gemini_api_base,
                recognition_model=settings.gemini_recognition_model,
            )
        else:
            ai_provider = FakeAIProvider()
    app.state.ai_provider = ai_provider

    app.add_middleware(RequestBoundary, max_body=settings.max_request_bytes)
    for router in (accounts.router, billing.router, submissions.router):
        app.include_router(router, prefix="/v1", responses={
            422: {"model": ErrorEnvelope, "description": "Ошибка валидации запроса"},
            500: {"model": ErrorEnvelope, "description": "Внутренняя ошибка"},
        })

    @app.exception_handler(ApiError)
    async def application_error(_request: Request, error: ApiError):
        headers = {"WWW-Authenticate": "Bearer"} if error.code == "authentication_required" else None
        return error_response(error.status_code, error.code, error.message, headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        return error_response(422, "validation_error", "Проверьте поля запроса.")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException):
        return error_response(error.status_code, "http_error", "Запрос не выполнен.", error.headers)

    @app.get("/health/live", tags=["Служебное"])
    def liveness():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Служебное"])
    def readiness(request: Request):
        state = request.app.state

        def check_database():
            with state.session_factory() as db:
                db.execute(text("SELECT 1 FROM submissions LIMIT 1"))

        checks = {
            "postgres": check_database,
            "redis": state.queue.ping,
            "object_storage": state.storage.ping,
        }
        dependencies = {}
        for name, check in checks.items():
            try:
                check()
            except Exception:
                dependencies[name] = "unavailable"
            else:
                dependencies[name] = "ok"
        ready = all(value == "ok" for value in dependencies.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ok" if ready else "unavailable", "dependencies": dependencies},
        )

    return app

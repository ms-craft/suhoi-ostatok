import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
job_context: ContextVar[dict | None] = ContextVar("job_context", default=None)
logger = logging.getLogger("stopkadr")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                **getattr(record, "event_fields", {}),
            },
            ensure_ascii=False,
        )


def configure_logging(level: str) -> None:
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)


def event(name: str, *, level: int | str = logging.INFO, **fields) -> None:
    context = dict(job_context.get() or {})
    if request_id.get():
        context["request_id"] = request_id.get()
    if isinstance(level, str):
        level = getattr(logging, level.upper())
    logger.log(level, name, extra={"event_fields": {"component": "api", **context, **fields}})


def worker_heartbeat(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.touch()
    except OSError:
        event("worker_heartbeat_write_failed", component="worker", level="WARNING",
              error_code="heartbeat_write_failed")

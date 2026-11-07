"""Coordinate S3 writes with operator-initiated orphan cleanup."""

from sqlalchemy import text
from sqlalchemy.orm import Session

OBJECT_WRITER_LOCK = 0x53554B484F594F52


def lock_object_writer(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": OBJECT_WRITER_LOCK})


def try_lock_object_cleanup(db: Session) -> bool:
    if db.get_bind().dialect.name != "postgresql":
        return False
    return bool(db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": OBJECT_WRITER_LOCK}))

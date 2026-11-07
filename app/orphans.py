"""Count and optionally remove old unreferenced objects from a dedicated bucket."""

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import timedelta
from itertools import islice

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import as_utc, utcnow
from app.db import make_session_factory
from app.object_lock import try_lock_object_cleanup
from app.storage import S3Storage, StoredObject
from app.submission_models import SourceFile, SubmissionPage

_UUID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
_KEY = re.compile(rf"submissions/{_UUID}/{_UUID}/(?:original/[1-9][0-9]*|pages/[1-9][0-9]*\.jpg)")


class CleanupUnavailable(Exception):
    pass


@dataclass
class Counts:
    scanned: int = 0
    candidates: int = 0
    removed: int = 0
    changed: int = 0
    scan_limited: bool = False
    delete_limited: bool = False


def _referenced(db: Session, key: str) -> bool:
    return bool(db.scalar(select(
        exists().where(SourceFile.object_key == key) | exists().where(SubmissionPage.object_key == key)
    )))


def _identity(item: StoredObject) -> tuple:
    # ListObjectsV2 may have fractional seconds, whereas HEAD uses HTTP dates.
    return (item.key, item.etag, item.size, as_utc(item.modified_at).replace(microsecond=0))


def reconcile(
    db: Session,
    storage: S3Storage,
    *,
    apply: bool = False,
    grace_hours: int = 48,
    max_scan: int = 5000,
    max_delete: int = 100,
) -> Counts:
    if grace_hours < 24 or not 1 <= max_scan <= 100_000 or not 1 <= max_delete <= 500:
        raise ValueError("Unsafe cleanup bounds")
    if apply and not try_lock_object_cleanup(db):
        raise CleanupUnavailable("Apply requires PostgreSQL and an idle object writer lock.")
    cutoff = utcnow() - timedelta(hours=grace_hours)
    counts = Counts()
    for item in islice(storage.iter_objects("submissions/"), max_scan + 1):
        if counts.scanned == max_scan:
            counts.scan_limited = True
            break
        counts.scanned += 1
        if not item.etag or not _KEY.fullmatch(item.key) or as_utc(item.modified_at) >= cutoff:
            continue
        if _referenced(db, item.key):
            continue
        counts.candidates += 1
        if not apply:
            continue
        if counts.removed == max_delete:
            counts.delete_limited = True
            break
        current = storage.inspect_object(item.key)
        if current is None or _identity(current) != _identity(item) or _referenced(db, item.key):
            counts.changed += 1
        elif storage.delete_unchanged(item):
            counts.removed += 1
        else:
            counts.changed += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Remove eligible objects after dry-run review.")
    parser.add_argument("--grace-hours", type=int, default=48, help="Ignore objects younger than this age (min 24).")
    parser.add_argument("--max-scan", type=int, default=5000)
    parser.add_argument("--max-delete", type=int, default=100)
    args = parser.parse_args()
    try:
        settings = get_settings()
        if settings.storage_backend != "s3":
            raise CleanupUnavailable("Orphan reconciliation requires S3 storage.")
        factory = make_session_factory(settings.database_url)
        with factory.begin() as db:
            counts = reconcile(db, S3Storage(settings), **vars(args))
    except CleanupUnavailable as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("Reconciliation interrupted. Check service availability and permissions before retrying.", file=sys.stderr)
        return 1
    print(json.dumps({"dry_run": not args.apply, **asdict(counts)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Local browser run with real Gemini and the local privacy sidecar.

This is a local-only runner: SQLite + in-memory storage/queue, real Gemini
provider, and PRIVACY_MODE=require. Start the privacy sidecar first:

    cd ..\\suhoi_ostatok\\backend
    npm run privacy

Then:

    $env:GEMINI_API_KEY = "sk-..."
    $env:GEMINI_API_BASE = "https://api.aiai.by/v1"
    .\\.venv\\Scripts\\python.exe -m tests.browser.privacy_fixture

and in another terminal:

    $env:API_ORIGIN = "http://127.0.0.1:58100"
    node site/serve.js
"""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import uvicorn

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.work import cleanup_due, due_jobs, run_job


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required")
    with TemporaryDirectory(prefix="stopkadr-privacy-browser-") as directory:
        database = (Path(directory) / "browser.db").as_posix()
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite:///{database}",
            storage_backend="memory",
            queue_backend="memory",
            ai_provider="gemini",
            gemini_api_key=api_key,
            gemini_api_base=os.environ.get("GEMINI_API_BASE", "https://api.aiai.by/v1"),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            gemini_recognition_model=os.environ.get("GEMINI_RECOGNITION_MODEL", "gemini-3.5-flash-lite"),
            gemini_humanities_model=os.environ.get("GEMINI_HUMANITIES_MODEL", "gemini-3.5-flash-lite"),
            privacy_mode="require",
            privacy_url=os.environ.get("PRIVACY_URL", "http://127.0.0.1:8790"),
            fake_payment_secret="local-browser-fixture-only",
            plans_json=json.dumps([{
                "code": "demo", "name": "Демонстрационный тариф",
                "checks_limit": 10, "max_pages": 10, "allow_followups": True,
            }]),
        )
        app = create_app(settings)
        state = app.state
        engine = state.session_factory.kw["bind"]
        Base.metadata.create_all(engine)
        stopping = Event()

        def work() -> None:
            while not stopping.is_set():
                job_id = state.queue.pop(1)
                if job_id:
                    run_job(state, job_id)
                for job_id in due_jobs(state):
                    run_job(state, job_id)
                cleanup_due(state)

        worker = Thread(target=work, daemon=True)
        worker.start()
        try:
            uvicorn.run(app, host="127.0.0.1", port=58100, access_log=False)
        finally:
            stopping.set()
            worker.join()
            engine.dispose()


if __name__ == "__main__":
    main()

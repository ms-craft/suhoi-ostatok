"""Local-only browser QA using the real API and worker with temporary test adapters."""

import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import sleep

import uvicorn
from PIL import Image

from app.ai import AIError, FakeAIProvider
from app.config import Settings
from app.db import Base
from app.main import create_app
from app.work import cleanup_due, due_jobs, run_job


class BrowserFixtureAI(FakeAIProvider):
    def __init__(self):
        super().__init__()
        self.failed_once: set[bytes] = set()

    def extract_questions(self, page):
        return ['Вычислите 2 + 2', 'Решите x + 1 = 3']

    def recognize(self, pages, clarification):
        # Distinct synthetic pages exercise terminal failure, retry and an empty result.
        sleep(1.6)
        with Image.open(BytesIO(pages[0].content)) as page:
            red, green, blue = page.convert("RGB").getpixel((0, 0))
        if (red, green, blue) == (255, 255, 255):
            return []
        if red >= 245 and green >= 245 and blue <= 20:
            digest = sha256(pages[0].content).digest()
            if digest not in self.failed_once:
                self.failed_once.add(digest)
                raise AIError("ai_unavailable")
        return super().recognize(pages, clarification)


def main():
    with TemporaryDirectory(prefix="stopkadr-browser-") as directory:
        database = (Path(directory) / "browser.db").as_posix()
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite:///{database}",
            storage_backend="memory",
            queue_backend="memory",
            ai_provider="fake",
            fake_payment_secret="local-browser-fixture-only",
            plans_json=json.dumps([{
                "code": "demo", "name": "Демонстрационный тариф",
                "checks_limit": 2, "max_pages": 10, "allow_followups": True,
            }]),
        )
        app = create_app(settings, ai_provider=BrowserFixtureAI())
        state = app.state
        engine = state.session_factory.kw["bind"]
        Base.metadata.create_all(engine)
        stopping = Event()

        def work():
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

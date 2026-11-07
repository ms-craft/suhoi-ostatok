from queue import Empty, Queue
from typing import Protocol

from redis import Redis

from app.config import Settings


class JobQueue(Protocol):
    def push(self, job_id: str) -> None: ...
    def pop(self, timeout: int) -> str | None: ...
    def ping(self) -> None: ...


class RedisJobQueue:
    def __init__(self, settings: Settings):
        self.key = settings.redis_queue_key
        self.client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=settings.worker_poll_seconds + 2,
        )

    def push(self, job_id: str) -> None:
        self.client.lpush(self.key, job_id)

    def pop(self, timeout: int) -> str | None:
        item = self.client.brpop(self.key, timeout)
        return item[1] if item else None

    def ping(self) -> None:
        self.client.ping()


class MemoryJobQueue:
    def __init__(self):
        self.queue: Queue[str] = Queue()

    def push(self, job_id: str) -> None:
        self.queue.put(job_id)

    def pop(self, timeout: int) -> str | None:
        try:
            return self.queue.get(timeout=timeout)
        except Empty:
            return None

    def ping(self) -> None:
        pass


def make_queue(settings: Settings) -> JobQueue:
    return MemoryJobQueue() if settings.queue_backend == "memory" else RedisJobQueue(settings)

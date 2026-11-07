import signal
from threading import Event
from time import monotonic

from app.config import get_settings
from app.core import event, worker_heartbeat
from app.main import create_app
from app.work import cleanup_due, due_jobs, run_job


def main() -> None:
    state = create_app(get_settings()).state
    if state.settings.queue_backend == "memory" or state.settings.storage_backend == "memory":
        raise RuntimeError("Separate workers require shared storage and a shared queue")
    stopped = Event()

    def stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    interval = state.settings.worker_poll_seconds
    next_sweep = 0.0
    event("worker_started", component="worker")
    while not stopped.is_set():
        try:
            if monotonic() >= next_sweep:
                cleanup_due(state)
                worker_heartbeat(state.settings.worker_heartbeat_path)
                for job_id in due_jobs(state):
                    if stopped.is_set():
                        break
                    run_job(state, job_id)
                next_sweep = monotonic() + interval
        except Exception:
            event("worker_sweep_failed", component="worker", level="ERROR", error_code="database_unavailable")
            next_sweep = monotonic() + interval
        if stopped.is_set():
            break
        try:
            job_id = state.queue.pop(interval)
        except Exception:
            event("queue_receive_failed", component="worker", level="WARNING", error_code="redis_unavailable")
            stopped.wait(interval)
            continue
        if job_id:
            try:
                run_job(state, job_id)
            except Exception:
                event("worker_job_dispatch_failed", component="worker", level="ERROR",
                      error_code="database_unavailable")
    event("worker_stopped", component="worker")


if __name__ == "__main__":
    main()

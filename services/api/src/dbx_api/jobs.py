"""Background processing with progress, and a cache of the replayed result.

Replaying a run is cheap once the model has spoken, but the first pass over a fresh
upload makes one model call per source column — forty-odd seconds for five files.
Doing that inside the request would time out and leave the consultant staring at a
spinner with nothing to read.

So processing runs in the background and reports what it is doing. The durability
story does not change: only decisions and deliveries are persisted, and the cached
result is an optimisation that is always safe to throw away.
"""
from __future__ import annotations

import datetime as dt
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Progress:
    stage: str = "queued"
    message: str = "Waiting to start"
    done: int = 0
    total: int = 0
    finished: bool = False
    failed: bool = False
    error: str | None = None
    started_at: str = field(
        default_factory=lambda: dt.datetime.now(dt.UTC).isoformat()
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "done": self.done,
            "total": self.total,
            "percent": round(100 * self.done / self.total) if self.total else 0,
            "finished": self.finished,
            "failed": self.failed,
            "error": self.error,
        }


class Jobs:
    """One worker per run, with the last good result kept for instant reads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._progress: dict[str, Progress] = {}
        self._results: dict[str, Any] = {}
        self._threads: dict[str, threading.Thread] = {}

    def progress(self, run_id: str) -> Progress:
        with self._lock:
            return self._progress.get(run_id, Progress(stage="idle", message="Ready",
                                                       finished=True))

    def result(self, run_id: str) -> Any | None:
        with self._lock:
            return self._results.get(run_id)

    def invalidate(self, run_id: str) -> None:
        """A decision changes the answer, so the cached result must go."""
        with self._lock:
            self._results.pop(run_id, None)

    def running(self, run_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(run_id)
        return bool(thread and thread.is_alive())

    def start(self, run_id: str, work: Callable[[Callable[..., None]], Any]) -> None:
        """Run `work` in the background, handing it a reporter to call as it goes."""
        if self.running(run_id):
            return

        def report(stage: str, message: str, done: int = 0, total: int = 0) -> None:
            with self._lock:
                p = self._progress.setdefault(run_id, Progress())
                p.stage, p.message = stage, message
                if total:
                    p.done, p.total = done, total

        def run() -> None:
            report("starting", "Reading the uploaded files")
            try:
                result = work(report)
                with self._lock:
                    self._results[run_id] = result
                    p = self._progress.setdefault(run_id, Progress())
                    p.stage, p.message = "done", "Finished"
                    p.finished = True
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
                with self._lock:
                    p = self._progress.setdefault(run_id, Progress())
                    p.stage, p.message = "failed", "Processing could not finish"
                    p.failed = p.finished = True
                    p.error = f"{exc}\n{traceback.format_exc(limit=3)}"

        with self._lock:
            self._progress[run_id] = Progress(stage="queued", message="Starting")
            thread = threading.Thread(target=run, name=f"run-{run_id}", daemon=True)
            self._threads[run_id] = thread
        thread.start()

    def wait(self, run_id: str, timeout: float = 120.0) -> None:
        """Block until the run settles. Used by tests and the synchronous fallback."""
        with self._lock:
            thread = self._threads.get(run_id)
        if thread:
            thread.join(timeout=timeout)


jobs = Jobs()

"""Background jobs with streamed output.

Every long action (helm install, repctl run, failover) becomes a Job so the UI can
show live output and keep a history that survives a page reload.
"""
from __future__ import annotations

import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from ..config import LOG_DIR, ensure_dirs, match_owner, tool_env

_jobs: dict[str, "Job"] = {}
_order: deque[str] = deque(maxlen=200)
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Job:
    def __init__(self, title: str, kind: str, cluster: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.kind = kind
        self.cluster = cluster
        self.status = "running"          # running | ok | failed
        self.started = _now()
        self.finished: Optional[str] = None
        self.lines: list[str] = []
        self.result: dict = {}
        self._event = threading.Event()
        ensure_dirs()
        self.log_path = LOG_DIR / f"{self.started[:10]}-{self.kind}-{self.id}.log"

    # -- output ---------------------------------------------------------------
    def log(self, line: str) -> None:
        stamped = line.rstrip("\n")
        with _lock:
            self.lines.append(stamped)
            if len(self.lines) > 4000:
                del self.lines[:1000]
        try:
            new_file = not self.log_path.exists()
            with self.log_path.open("a") as fh:
                fh.write(stamped + "\n")
            if new_file:
                match_owner(self.log_path)
        except OSError:
            pass
        self._event.set()

    def finish(self, ok: bool, **result) -> None:
        self.status = "ok" if ok else "failed"
        self.finished = _now()
        self.result.update(result)
        self._event.set()

    # -- helpers --------------------------------------------------------------
    def run(self, cmd: list[str], env: dict | None = None, cwd: str | None = None,
            stdin_text: str | None = None, echo: bool = True) -> int:
        """Run a command, streaming stdout+stderr into the job log."""
        if echo:
            self.log("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                text=True, env=env or tool_env(), cwd=cwd, bufsize=1,
            )
        except FileNotFoundError as exc:
            self.log(f"error: {exc}")
            return 127
        if stdin_text is not None and proc.stdin:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log(line)
        return proc.wait()

    def snapshot(self, after: int = 0) -> dict:
        with _lock:
            return {
                "id": self.id, "title": self.title, "kind": self.kind,
                "cluster": self.cluster, "status": self.status,
                "started": self.started, "finished": self.finished,
                "lines": self.lines[after:], "total": len(self.lines),
                "result": self.result,
            }

    def wait_for_output(self, timeout: float = 15.0) -> None:
        self._event.wait(timeout)
        self._event.clear()


def start(title: str, kind: str, target: Callable[[Job], None], cluster: str | None = None) -> Job:
    job = Job(title, kind, cluster)
    with _lock:
        _jobs[job.id] = job
        _order.append(job.id)

    def runner() -> None:
        try:
            target(job)
        except Exception as exc:                       # noqa: BLE001 - surfaced in the UI
            job.log(f"error: {exc}")
            job.finish(False, error=str(exc))
        else:
            if job.status == "running":
                job.finish(True)

    threading.Thread(target=runner, daemon=True, name=f"job-{job.id}").start()
    return job


class ConsoleJob(Job):
    """A job that prints as it goes, for the command line tool."""

    def log(self, line: str) -> None:
        super().log(line)
        print(line.rstrip("\n"), flush=True)


def run_sync(title: str, kind: str, target: Callable[["Job"], None],
             cluster: str | None = None) -> Job:
    """Run a job in the foreground and return it when finished."""
    job = ConsoleJob(title, kind, cluster)
    with _lock:
        _jobs[job.id] = job
        _order.append(job.id)
    try:
        target(job)
    except Exception as exc:                          # noqa: BLE001
        job.log(f"error: {exc}")
        job.finish(False, error=str(exc))
    else:
        if job.status == "running":
            job.finish(True)
    return job


def get(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def recent(limit: int = 20) -> list[Job]:
    with _lock:
        ids = list(_order)[-limit:][::-1]
    return [_jobs[i] for i in ids if i in _jobs]


def running() -> Iterable[Job]:
    return [j for j in _jobs.values() if j.status == "running"]

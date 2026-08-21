"""
Track detached, long-running subprocesses across pipeline invocations.

A production foundry campaign runs for days — RF3 alone took 3.7 days on the
reference CD79b run. That cannot be a blocking `subprocess.run` inside a pipeline
stage: under Celery the visibility timeout expires and the task is redelivered to
a second worker, so two RF3 runs race for one GPU; under the CLI, closing the
terminal kills the campaign.

So jobs are launched detached (`start_new_session=True`, the `nohup setsid`
equivalent), recorded in a JSON registry beside their output, and polled. A later
process re-reads the registry and re-attaches instead of relaunching.

**Liveness is checked by PID *and* process start time.** PIDs are recycled; after
a reboot a bare `os.kill(pid, 0)` will cheerfully report someone else's process
as your campaign, and the pipeline would then wait forever for output that is
never coming.
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from loguru import logger

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_ORPHANED = "orphaned"     # the process is gone and we never saw it exit


@dataclass
class JobRecord:
    job_id: str
    pid: int
    pid_start_ticks: int
    argv: list[str]
    cwd: str
    log_path: str
    started_at: float
    finished_at: float | None = None
    returncode: int | None = None
    status: str = STATUS_RUNNING
    note: str = ""

    @property
    def elapsed_s(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """
    (state, start_ticks) from /proc/<pid>/stat, or None if the pid is gone.

    Start time (field 22) plus the pid identifies a process RUN uniquely, which a
    bare pid does not once the number has been recycled. State (field 3) is
    needed because a finished child we have not reaped is a zombie: it still has
    a /proc entry and still accepts signal 0, so both of the obvious liveness
    checks say it is running.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The comm field is parenthesised and may itself contain spaces or brackets,
    # so split after the LAST ')' rather than on whitespace.
    tail = stat[stat.rfind(")") + 1:].split()
    try:
        return tail[0], int(tail[19])     # state, then field 22 overall
    except (IndexError, ValueError):
        return None


def _proc_start_ticks(pid: int) -> int | None:
    st = _proc_stat(pid)
    return st[1] if st else None


def is_alive(rec: JobRecord) -> bool:
    """True only if the recorded process is still the one running under that pid."""
    if rec.pid <= 0:
        return False
    try:
        os.kill(rec.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                          # exists, owned by someone else
    st = _proc_stat(rec.pid)
    if st is None:
        return False
    state, ticks = st
    if state == "Z":
        return False                  # exited, just not reaped yet
    if rec.pid_start_ticks <= 0:
        return True                   # nothing to verify against; trust the pid
    return ticks == rec.pid_start_ticks


class JobRegistry:
    """A tiny JSON-backed registry of detached jobs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.jobs: dict[str, JobRecord] = {}
        # Live Popen handles for jobs THIS process started. Python's subprocess
        # module reaps finished children opportunistically whenever a new Popen
        # is created, so by the time we call waitpid the child is often already
        # gone and the exit code is unrecoverable. Holding the handle and using
        # poll() is the only reliable way to get a return code in-process.
        self._procs: dict[str, Any] = {}
        self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"unreadable job registry {self.path}: {exc}")
            return
        for job_id, d in (raw.get("jobs") or {}).items():
            try:
                self.jobs[job_id] = JobRecord(**d)
            except TypeError:
                logger.warning(f"skipping malformed job record {job_id!r}")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"jobs": {k: asdict(v) for k, v in self.jobs.items()}},
                       indent=2),
            encoding="utf-8")
        os.replace(tmp, self.path)      # atomic, same idiom as src/project.py

    # -- lifecycle -----------------------------------------------------

    def launch(
        self,
        job_id: str,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        log_path: str | Path,
        env: dict[str, str] | None = None,
        note: str = "",
    ) -> JobRecord:
        """
        Start a detached process and record it.

        `start_new_session=True` puts the child in its own session so it survives
        the parent exiting — the same effect as `nohup setsid`, which is how the
        reference campaign driver was launched.
        """
        import subprocess

        existing = self.jobs.get(job_id)
        if existing is not None and is_alive(existing):
            logger.info(f"job {job_id} already running (pid {existing.pid}); attaching")
            return existing
        self._procs.pop(job_id, None)

        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        merged = {**os.environ, **(env or {})}
        handle = log_path.open("a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                list(argv), cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=merged,
                start_new_session=True, close_fds=True,
            )
        finally:
            handle.close()

        self._procs[job_id] = proc
        rec = JobRecord(
            job_id=job_id, pid=proc.pid,
            pid_start_ticks=_proc_start_ticks(proc.pid) or 0,
            argv=list(argv), cwd=str(cwd), log_path=str(log_path),
            started_at=time.time(), note=note,
        )
        self.jobs[job_id] = rec
        self._save()
        logger.info(f"launched {job_id} as pid {proc.pid} -> {log_path}")
        return rec

    def refresh(self, job_id: str) -> JobRecord:
        """
        Re-check a job's liveness and reap its exit status.

        A process that has vanished without us seeing an exit code is marked
        `orphaned`, not `done` — the caller must decide from the output on disk
        whether the work actually finished, because "the process is gone" and
        "the work is complete" are different claims.
        """
        rec = self.jobs.get(job_id)
        if rec is None:
            raise KeyError(f"no job {job_id!r} in {self.path}")
        if rec.status != STATUS_RUNNING:
            return rec
        if is_alive(rec):
            return rec

        rec.finished_at = time.time()
        proc = self._procs.get(job_id)
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                rc = proc.wait(timeout=5)
            rec.returncode = rc
            rec.status = STATUS_DONE if rc == 0 else STATUS_FAILED
        else:
            # A job started by a different process (a resumed run). Its exit code
            # is unrecoverable, so say so rather than guessing: "the process is
            # gone" is not the same claim as "the work finished", and only the
            # output on disk can settle that.
            try:
                pid, status = os.waitpid(rec.pid, os.WNOHANG)
                if pid == rec.pid:
                    rec.returncode = os.waitstatus_to_exitcode(status)
                    rec.status = (STATUS_DONE if rec.returncode == 0
                                  else STATUS_FAILED)
                else:
                    rec.status = STATUS_ORPHANED
            except (ChildProcessError, OSError):
                rec.status = STATUS_ORPHANED
        self._save()
        logger.info(
            f"job {job_id} finished: {rec.status} "
            f"(rc={rec.returncode}, {rec.elapsed_s / 3600:.2f} h)")
        return rec

    def get(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    def kill(self, job_id: str, sig: int = signal.SIGTERM) -> bool:
        """Signal the job's whole process group — the driver spawns children."""
        rec = self.jobs.get(job_id)
        if rec is None or not is_alive(rec):
            return False
        try:
            os.killpg(os.getpgid(rec.pid), sig)
        except OSError:
            try:
                os.kill(rec.pid, sig)
            except OSError:
                return False
        logger.info(f"signalled {job_id} (pid {rec.pid}) with {sig}")
        return True

    def poll_until(
        self,
        job_id: str,
        *,
        until: Callable[[], bool],
        tick_s: float = 120.0,
        on_tick: Callable[[JobRecord], None] | None = None,
        timeout_s: float | None = None,
    ) -> JobRecord:
        """
        Block until `until()` is satisfied or the job stops running.

        `until` is a DISK check, not a process check: the process exiting is not
        proof the work finished (RFD3 is restarted several times by its own retry
        loop), and the work finishing is not proof the process exited.
        """
        start = time.time()
        while True:
            rec = self.refresh(job_id)
            if until():
                return rec
            if rec.status != STATUS_RUNNING:
                return rec
            if timeout_s is not None and time.time() - start > timeout_s:
                logger.warning(f"job {job_id} still running after {timeout_s / 3600:.1f} h")
                return rec
            if on_tick:
                on_tick(rec)
            time.sleep(tick_s)

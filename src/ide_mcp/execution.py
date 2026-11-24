"""Shared run ownership, native workspace leases and bounded in-process jobs."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

from . import common, guard
from .common import Blocked, OutcomeError, active, sanitise

MAX_JOBS = 64
JOB_TTL = 1800
MAX_LOG = 1_000_000


def source_identity(root: Path) -> dict[str, Any]:
    """Git metadata only. No source diff or protected content is opened."""
    root = active().launch_dir(root)
    if common.worktree_root(root) is None:
        return {"sha": None, "dirty": None, "stability": "unverified"}
    try:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, timeout=10, env=env, stdin=subprocess.DEVNULL, check=False)
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
                                capture_output=True, timeout=10, env=env, stdin=subprocess.DEVNULL, check=False)
        return {"sha": sha.stdout.decode().strip() if sha.returncode == 0 else None,
                "dirty": bool(status.stdout) if status.returncode == 0 else None, "stability": "unverified"}
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return {"sha": None, "dirty": None, "stability": "unverified"}


def _runtime_scope(scope: guard.Scope, root: Path) -> guard.Scope:
    """Keep execution bookkeeping in a guarded temporary directory, never a worktree."""
    root = scope.dir(root)
    temporary = guard.Scope.of(tempfile.gettempdir())
    if temporary.worktree is not None or common.within(temporary.root, scope.worktree or root):
        raise Blocked("temporary_directory_in_workspace", "Select an operating-system temporary directory outside worktrees.", "environment")
    # dover-gain-efficient: a stable path shares native locks across server processes.
    key = hashlib.sha256(os.fsencode(os.path.normcase(str(root)))).hexdigest()
    directory = temporary.make_dirs(temporary.root / "ide-mcp" / key)
    return guard.Scope.of(str(directory))


def save_artifact(text: str, *, output_scope: guard.Scope | None = None) -> dict[str, Any]:
    """Persist only sanitised bytes in temporary storage with a unique name."""
    scope = output_scope or active()
    payload = sanitise(text).encode("utf-8")
    try:
        scope = _runtime_scope(scope, scope.root)
        path = scope.check(scope.root / "logs" / f"{uuid.uuid4().hex}.safe.log")
        scope.write_bytes(path, payload, expected_revision="-")
        own = sorted(path.parent.glob("*.safe.log"), key=lambda p: p.stat().st_mtime)
        for old in own[:-30]:
            scope.check(old).unlink()
        return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(),
                "verified": True, "sanitised": True, "output_scope": str(scope.root)}
    except (OSError, OutcomeError) as e:
        return {"path": None, "verified": False, "reason": "artifact_write_failed", "detail": sanitise(str(e))}


class WorkspaceLease:
    """A native file lock shared by competing stdio instances, held through report capture."""

    def __init__(self, root: Path):
        self.root, self.fd = root, None

    async def __aenter__(self):
        scope = _runtime_scope(active(), self.root)
        path = scope.check(scope.root / "validation.lock")
        scope.make_dirs(path.parent)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            scope._verify_open(self.fd, path, expect_regular=True)
            while True:
                try:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    if common.WINDOWS:
                        import msvcrt
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise Blocked("workspace_lease_unavailable", f"Native workspace locking failed: {e}", "environment") from None
                    await asyncio.sleep(0.1)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    async def __aexit__(self, *_exc):
        if self.fd is not None:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if common.WINDOWS:
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


@dataclass
class Job:
    id: str
    scope: guard.Scope
    state: str = "pending"
    created: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    final: dict[str, Any] | None = None
    log: str = ""
    base: int = 0

    def append(self, text: str):
        self.log += sanitise(text) + "\n"
        if len(self.log) > MAX_LOG:
            removed = len(self.log) - MAX_LOG
            self.log = self.log[removed:]
            self.base += removed


class Jobs:
    """Jobs live until expiry or server exit. Unknown IDs never resume or relaunch work."""

    def __init__(self):
        self.jobs: dict[str, Job] = {}

    def start(self, work: Callable[[], Awaitable[Any]]) -> Job:
        self.expire()
        if len(self.jobs) >= MAX_JOBS or sum(j.state != "finished" for j in self.jobs.values()) >= 8:
            raise Blocked("job_capacity", "The bounded job store is full. Wait for running work or expiry.", "environment")
        op = common._ACTIVE.get()
        scope = active("execution")
        job = Job(uuid.uuid4().hex, scope)
        self.jobs[job.id] = job

        async def worker():
            try:
                # Explicit binding; never depend on an inherited request ContextVar.
                with guard.activate(scope, op.needs, op.name):
                    scope.check(scope.root, protected=False)
                    job.state = "running"
                    token = common._OUTPUT_SINK.set(job.append)
                    try:
                        value = await work()
                    finally:
                        common._OUTPUT_SINK.reset(token)
                    if not job.log:
                        job.append(str(value))
                    job.final = value.fields if isinstance(value, guard.TextResult) else guard.result(
                        str(value), complete=False, incomplete_reason="consumer_completeness_unverified").fields
            except asyncio.CancelledError:
                job.final = {"outcome": "cancelled", "reason": "owned_execution_stopped", "complete": False}
            except Exception as e:  # noqa: BLE001 - retain unexpected worker failures as job results
                job.append(str(e))
                if getattr(e, "reason", None) == "termination_unconfirmed":
                    job.state = "unknown"
                    return
                job.final = getattr(e, "fields", None) or {"outcome": getattr(e, "outcome", "failed"),
                             "reason": getattr(e, "reason", "job_failed"),
                             "category": getattr(e, "category", "unknown"),
                             "result": sanitise(str(e)), "complete": False}
            finally:
                if job.final is not None:
                    job.state = "finished"
        job.task = asyncio.create_task(worker())
        return job

    def cancel(self, job: Job) -> None:
        if job.state == "pending":
            if job.task:
                job.task.cancel()
            job.state, job.final = "finished", {"outcome": "cancelled", "reason": "cancelled_before_launch", "complete": False}
        elif job.state == "running":
            job.state = "cancelling"
            if job.task:
                job.task.cancel()

    def expire(self):
        now = time.monotonic()
        for key, job in list(self.jobs.items()):
            if job.state == "finished" and now - job.created > JOB_TTL:
                del self.jobs[key]

    def get(self, job_id: str) -> Job:
        self.expire()
        job = self.jobs.get(job_id)
        if job is None:
            raise Blocked("job_unknown", "Job ID is unknown or expired, possibly after a server restart. No work was relaunched.", "input")
        scope = active()
        if scope.ident != job.scope.ident or scope.worktree != job.scope.worktree:
            raise Blocked("job_scope_mismatch", "This job belongs to another selected scope.")
        scope.check(scope.root, protected=False)
        return job

    def output(self, job: Job, cursor: int, limit: int) -> dict[str, Any]:
        end = job.base + len(job.log)
        if cursor < job.base:
            raise Blocked("cursor_expired", f"History before cursor {job.base} was discarded. Continue explicitly from there.", "input")
        if cursor > end or cursor < 0 or not 1 <= limit <= 30_000:
            raise Blocked("invalid_cursor", "The cursor or page limit is invalid.", "input")
        text = job.log[cursor - job.base:cursor - job.base + limit]
        return {"text": text, "cursor": cursor + len(text), "history_start": job.base,
                "history_complete": job.base == 0, "has_more": cursor + len(text) < end}


def register_jobs(ops: guard.Operations) -> Jobs:
    jobs = Jobs()
    read = ToolAnnotations(readOnlyHint=True)

    @ops.tool(kind="execution", needs=(guard.EXECUTION,), annotations=read)
    async def job_status(job_id: str, expected_root: guard.ExpectedRoot = None) -> str:
        job = jobs.get(job_id)
        return guard.result(job.state, complete=job.state == "finished",
                            incomplete_reason=None if job.state == "finished" else "job_not_final",
                            data={"job_id": job.id, "state": job.state,
                                  "final_outcome": job.final.get("outcome") if job.final else None})

    @ops.tool(kind="execution", needs=(guard.EXECUTION,), annotations=read)
    async def job_output(job_id: str, cursor: int = 0, limit: int = 30_000,
                         expected_root: guard.ExpectedRoot = None) -> str:
        page = jobs.output(jobs.get(job_id), cursor, limit)
        return guard.result(page["text"], complete=not page["has_more"] and page["history_complete"],
                            incomplete_reason="bounded_history" if page["has_more"] or not page["history_complete"] else None,
                            continuation={"cursor": page["cursor"]}, data=page)

    @ops.tool(kind="execution", needs=(guard.EXECUTION,), annotations=ToolAnnotations(readOnlyHint=False))
    async def cancel_job(job_id: str, expected_root: guard.ExpectedRoot = None) -> str:
        job = jobs.get(job_id)
        jobs.cancel(job)
        return guard.result(job.state, complete=job.state == "finished",
                            incomplete_reason=None if job.state == "finished" else "cancellation_pending",
                            data={"job_id": job.id, "state": job.state})

    @ops.tool(kind="execution", needs=(guard.EXECUTION,), annotations=read)
    async def job_result(job_id: str, expected_root: guard.ExpectedRoot = None) -> str:
        job = jobs.get(job_id)
        if job.final is None:
            raise Blocked("job_not_final", f"The job is {job.state}; no verified final result exists.", "environment")
        f = job.final
        return guard.result(f.get("result", f["outcome"]), outcome=f["outcome"], reason=f.get("reason"),
                            category=f.get("category"), complete=f.get("complete", False),
                            incomplete_reason=f.get("incomplete_reason"), data=f)

    @ops.tool(kind="file", needs=(guard.TARGET,), annotations=read)
    async def read_artifact(path: str, sha256: str, start: int = 0, limit: int = 30_000,
                            expected_root: guard.ExpectedRoot = None) -> str:
        """Retrieve verified sanitised output. Select its explicit output scope as expected_root."""
        scope = active()
        p = scope.file(path)
        if p.suffix != ".log" or not p.name.endswith(".safe.log"):
            raise Blocked("artifact_unverified", "This route does not retrieve older or raw logs.")
        data = scope.read_bytes(p, 20_000_000)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise Blocked("artifact_changed", "The sanitised artifact does not match its supplied hash.", "input")
        if start < 0 or not 1 <= limit <= 30_000:
            raise Blocked("invalid_cursor", "Invalid artifact range.", "input")
        text = sanitise(data.decode("utf-8"))
        end = min(len(text), start + limit)
        return guard.result(text[start:end], complete=end == len(text),
                            incomplete_reason=None if end == len(text) else "range_limited",
                            continuation={"start": end} if end < len(text) else None,
                            data={"sha256": sha256, "verified": True})
    return jobs


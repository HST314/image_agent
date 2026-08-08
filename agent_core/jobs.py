"""Durable, process-safe asynchronous workflow jobs."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from storage.project_store import atomic_json, content_hash
from agent_core.error_taxonomy import error_record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_start(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        return "unknown"


def _owner_alive(record: dict[str, Any]) -> bool:
    pid = record.get("owner_pid")
    return isinstance(pid, int) and record.get("owner_start") == _process_start(pid) != "unknown"


class JobStore:
    """Atomic per-project registry; idempotency keys are immutable bindings."""

    def __init__(self, project_root: Path, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1。")
        self.path = project_root / "runtime/jobs.json"
        self.lock_path = project_root / "runtime/jobs.lock"
        self.max_attempts = max_attempts

    def _locked(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"version": 2, "jobs": {}}
            # Read-time, locked migration keeps pre-P1 job files resumable and
            # makes every subsequently returned record satisfy AsyncJob v1.
            data["version"] = 2
            for job in data.setdefault("jobs", {}).values():
                job.setdefault("cancel_requested", job.get("status") == "cancel_requested")
                job.setdefault("progress", {"completed": 0, "total": 1, "unit": "workflow"})
                job.setdefault("heartbeat_at", None)
                job.setdefault("attempt", 0)
                job.setdefault("max_attempts", self.max_attempts)
                legacy_error = job.get("error")
                if legacy_error and "stage" not in legacy_error:
                    message = legacy_error.get("message") or legacy_error.get("code") or "legacy job failure"
                    normalized = error_record(RuntimeError(message), stage="workflow")
                    normalized["retryable"] = bool(legacy_error.get("retryable", normalized["retryable"]))
                    normalized["suggested_action"] = "retry" if normalized["retryable"] else "contact_admin"
                    job["error"] = normalized
            result = mutate(data)
            atomic_json(self.path, data)
            return result
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def create(self, idempotency_key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        payload_hash = content_hash(payload)
        def mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            for job in data["jobs"].values():
                if job["idempotency_key"] == idempotency_key:
                    if job["payload_hash"] != payload_hash:
                        raise ValueError("同一作业幂等键不能提交不同参数。")
                    if (job["status"] == "failed"
                            and bool((job.get("error") or {}).get("retryable", True))
                            and int(job.get("attempt", 0)) < int(job.get("max_attempts", self.max_attempts))):
                        job.update(status="queued", error=None, updated_at=_now())
                    return dict(job), False
            job_id = f"job_{uuid4().hex}"
            job = {"job_id": job_id, "idempotency_key": idempotency_key, "payload_hash": payload_hash,
                   "payload": payload, "status": "queued", "cancel_requested": False,
                   "progress": {"completed": 0, "total": 1, "unit": "workflow"},
                   "heartbeat_at": None, "created_at": _now(), "updated_at": _now(), "attempt": 0,
                   "max_attempts": self.max_attempts}
            data["jobs"][job_id] = job
            return dict(job), True
        return self._locked(mutate)

    def get(self, job_id: str) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            if job_id not in data["jobs"]:
                raise FileNotFoundError("作业不存在。")
            return dict(data["jobs"][job_id])
        return self._locked(mutate)

    def claim(self, job_id: str) -> dict[str, Any] | None:
        def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
            job = data["jobs"].get(job_id)
            if not job or job["status"] in {"succeeded", "failed", "cancelled"}:
                return None
            if job.get("cancel_requested"):
                job.update(status="cancelled", finished_at=_now(), updated_at=_now())
                return None
            if job["status"] == "running" and _owner_alive(job):
                return None
            job.update(status="running", owner_pid=os.getpid(), owner_start=_process_start(os.getpid()),
                       started_at=_now(), heartbeat_at=_now(), updated_at=_now(),
                       attempt=int(job.get("attempt", 0)) + 1)
            return dict(job)
        return self._locked(mutate)

    def heartbeat(self, job_id: str, *, completed: int, total: int, unit: str) -> dict[str, Any]:
        """Persist authoritative progress; workers must not manufacture time-based percentages."""
        if total < 1 or completed < 0 or completed > total:
            raise ValueError("作业进度必须满足 0 <= completed <= total 且 total >= 1。")
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            job = data["jobs"][job_id]
            if job["status"] not in {"running", "cancel_requested"}:
                raise ValueError("只有运行中或请求取消的作业可以更新心跳。")
            now = _now()
            job.update(progress={"completed": completed, "total": total, "unit": unit},
                       heartbeat_at=now, updated_at=now)
            return dict(job)
        return self._locked(mutate)

    def finish(self, job_id: str, *, error: dict[str, Any] | None = None) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            job = data["jobs"][job_id]
            status = "cancelled" if job.get("cancel_requested") else ("failed" if error else "succeeded")
            final_error = None if status == "cancelled" else error
            if status == "succeeded":
                progress = job.get("progress") or {"total": 1, "unit": "workflow"}
                job["progress"] = {**progress, "completed": progress["total"]}
            job.update(status=status, error=final_error, finished_at=_now(), updated_at=_now())
            return dict(job)
        return self._locked(mutate)

    def cancel(self, job_id: str) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            if job_id not in data["jobs"]:
                raise FileNotFoundError("作业不存在。")
            job = data["jobs"][job_id]
            if job["status"] not in {"succeeded", "failed", "cancelled"}:
                job["cancel_requested"] = True
                if job["status"] == "queued":
                    job.update(status="cancelled", finished_at=_now())
                else:
                    job["status"] = "cancel_requested"
                job["updated_at"] = _now()
            return dict(job)
        return self._locked(mutate)

    def cancellation_requested(self, job_id: str) -> bool:
        """Cheap cooperative-cancellation probe for unit boundaries."""
        return bool(self.get(job_id).get("cancel_requested"))

    def recoverable(self) -> list[str]:
        def mutate(data: dict[str, Any]) -> list[str]:
            result = []
            for job in data["jobs"].values():
                if job["status"] == "queued" or (job["status"] in {"running", "cancel_requested"} and not _owner_alive(job)):
                    if job.get("cancel_requested"):
                        job.update(status="cancelled", finished_at=_now(), updated_at=_now())
                        continue
                    job.update(status="queued", updated_at=_now())
                    result.append(job["job_id"])
            return result
        return self._locked(mutate)

    def active(self) -> list[dict[str, Any]]:
        """Return jobs which make moving the project's execution head unsafe."""
        def mutate(data: dict[str, Any]) -> list[dict[str, Any]]:
            return [dict(job) for job in data["jobs"].values()
                    if job.get("status") in {"queued", "running", "cancel_requested"}]
        return self._locked(mutate)


class WorkflowJobWorker:
    def __init__(self, execute: Callable[[str, dict[str, Any]], None], *, workers: int = 2) -> None:
        self.execute = execute
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="workflow-job")
        self._submitted: set[tuple[str, str]] = set()
        self._guard = threading.Lock()

    def submit(self, project_id: str, job_id: str) -> None:
        key = (project_id, job_id)
        with self._guard:
            if key in self._submitted:
                return
            self._submitted.add(key)
        def run() -> None:
            try:
                self.execute(project_id, {"job_id": job_id})
            finally:
                with self._guard:
                    self._submitted.discard(key)
        self.pool.submit(run)

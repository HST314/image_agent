"""Side-effect-free, cached health and privileged diagnostic probes."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yaml

LOGGER = logging.getLogger("image_agent.health")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HealthPolicy:
    cache_ttl_seconds: float = 5.0
    probe_timeout_seconds: float = 1.0
    worker_stale_seconds: float = 120.0
    queue_stale_seconds: float = 300.0
    storage_min_free_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "HealthPolicy":
        def number(name: str, default: float, minimum: float) -> float:
            raw = os.getenv(name)
            value = default if raw is None else float(raw)
            if value < minimum:
                raise ValueError(f"{name} is below its safe minimum")
            return value
        return cls(
            cache_ttl_seconds=number("IMAGE_AGENT_HEALTH_CACHE_TTL_SECONDS", 5.0, 0.1),
            probe_timeout_seconds=number("IMAGE_AGENT_HEALTH_PROBE_TIMEOUT_SECONDS", 1.0, 0.05),
            worker_stale_seconds=number("IMAGE_AGENT_HEALTH_WORKER_STALE_SECONDS", 120.0, 1.0),
            queue_stale_seconds=number("IMAGE_AGENT_HEALTH_QUEUE_STALE_SECONDS", 300.0, 1.0),
            storage_min_free_bytes=int(number("IMAGE_AGENT_HEALTH_STORAGE_MIN_FREE_BYTES", 256 * 1024 * 1024, 1)),
        )


class HealthService:
    """Runs bounded read-only probes; concurrent callers share one refresh."""

    def __init__(self, projects_root: Path, model_config: Path, *, policy: HealthPolicy | None = None,
                 provider_configured: Callable[[], bool] = lambda: False,
                 asset_proxy_configured: Callable[[], bool] = lambda: True,
                 probes: dict[str, Callable[[], dict[str, Any]]] | None = None) -> None:
        self.projects_root = projects_root
        self.model_config = model_config
        self.policy = policy or HealthPolicy.from_env()
        self.provider_configured = provider_configured
        self.asset_proxy_configured = asset_proxy_configured
        self._condition = threading.Condition()
        self._refreshing = False
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._internal: dict[str, dict[str, Any]] = {}
        self._probes = probes or {
            "model_router": self._probe_model_router,
            "worker": self._probe_worker,
            "queue": self._probe_queue,
            "storage": self._probe_storage,
            "event_append": self._probe_event_append,
            "asset_proxy": self._probe_asset_proxy,
        }

    def liveness(self) -> dict[str, Any]:
        trace_id = f"trace_{uuid4().hex}"
        return {"status": "alive", "checked_at": _now(), "trace_id": trace_id}

    def readiness(self) -> dict[str, Any]:
        with self._condition:
            now = time.monotonic()
            if self._cached is not None and now - self._cached_at < self.policy.cache_ttl_seconds:
                return dict(self._cached)
            if self._refreshing:
                self._condition.wait_for(lambda: not self._refreshing,
                                         timeout=self.policy.probe_timeout_seconds * 2)
                if self._cached is not None:
                    return dict(self._cached)
            self._refreshing = True
        try:
            result = self._refresh()
            with self._condition:
                self._cached, self._cached_at = result, time.monotonic()
            return dict(result)
        finally:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()

    def diagnostics(self, trace_id: str) -> dict[str, Any] | None:
        with self._condition:
            value = self._internal.get(trace_id)
            return json.loads(json.dumps(value)) if value else None

    def _refresh(self) -> dict[str, Any]:
        trace_id, checked_at = f"trace_{uuid4().hex}", _now()
        executor = ThreadPoolExecutor(max_workers=len(self._probes), thread_name_prefix="health-probe")
        futures = {executor.submit(probe): name for name, probe in self._probes.items()}
        done, pending = wait(futures, timeout=self.policy.probe_timeout_seconds)
        external: dict[str, Any] = {}
        internal: dict[str, Any] = {"trace_id": trace_id, "checked_at": checked_at, "components": {}}
        for future in done:
            name = futures[future]
            try:
                raw = future.result()
                external[name] = {key: raw[key] for key in ("status", "error_code", "checked_at", "business_impact")}
                internal["components"][name] = raw
            except Exception as exc:
                external[name] = self._external(name, "unhealthy", "PROBE_FAILED", "该组件暂不可用")
                internal["components"][name] = {"exception_type": type(exc).__name__, "exception": str(exc)}
        for future in pending:
            name = futures[future]
            future.cancel()
            external[name] = self._external(name, "unhealthy", "PROBE_TIMEOUT", "该组件检查超时，暂不可用")
            internal["components"][name] = {"exception_type": "TimeoutError", "timeout_seconds": self.policy.probe_timeout_seconds}
        executor.shutdown(wait=False, cancel_futures=True)
        statuses = {item["status"] for item in external.values()}
        critical = {external[name]["status"] for name in ("storage", "event_append") if name in external}
        status = "not_ready" if "unhealthy" in critical else ("degraded" if statuses - {"healthy"} else "ready")
        result = {"status": status, "checked_at": checked_at, "trace_id": trace_id, "components": external}
        with self._condition:
            self._internal[trace_id] = internal
            while len(self._internal) > 100:
                self._internal.pop(next(iter(self._internal)))
        LOGGER.info("health readiness completed", extra={"trace_id": trace_id, "health_status": status})
        return result

    @staticmethod
    def _external(name: str, status: str, code: str, impact: str) -> dict[str, Any]:
        return {"status": status, "error_code": code, "checked_at": _now(), "business_impact": impact}

    def _project_modes_and_jobs(self) -> tuple[set[str], list[dict[str, Any]]]:
        modes: set[str] = set()
        jobs: list[dict[str, Any]] = []
        if not self.projects_root.is_dir():
            return modes, jobs
        for child in self.projects_root.iterdir():
            if not child.is_dir():
                continue
            project_file = child / "project.yaml"
            jobs_file = child / "runtime/jobs.json"
            try:
                if project_file.is_file():
                    project = json.loads(project_file.read_text(encoding="utf-8"))
                    modes.add(str((project.get("runtime_policy") or {}).get("mode") or "unknown"))
                if jobs_file.is_file():
                    jobs.extend((json.loads(jobs_file.read_text(encoding="utf-8")).get("jobs") or {}).values())
            except (OSError, ValueError, TypeError):
                modes.add("unknown")
        return modes, jobs

    def _probe_model_router(self) -> dict[str, Any]:
        data = yaml.safe_load(self.model_config.read_text(encoding="utf-8"))
        bindings = data.get("state_bindings") if isinstance(data, dict) else None
        if not isinstance(bindings, list) or not bindings:
            raise ValueError("model route bindings are missing")
        modes, _ = self._project_modes_and_jobs()
        if not self.provider_configured():
            if modes and modes <= {"offline"}:
                return self._external("model_router", "offline", "OFFLINE_ONLY", "仅离线项目可运行，不代表生产供应商健康")
            return self._external("model_router", "not_configured", "MODEL_PROVIDER_NOT_CONFIGURED", "真实模型调用不可用")
        return self._external("model_router", "healthy", "OK", "模型路由配置可用于真实调用")

    def _probe_worker(self) -> dict[str, Any]:
        _, jobs = self._project_modes_and_jobs()
        now = datetime.now(timezone.utc)
        stale = []
        for job in jobs:
            if job.get("status") in {"running", "cancel_requested"}:
                try:
                    age = (now - datetime.fromisoformat(job["heartbeat_at"])).total_seconds()
                    if age > self.policy.worker_stale_seconds: stale.append(job.get("job_id"))
                except (KeyError, TypeError, ValueError): stale.append(job.get("job_id"))
        return self._external("worker", "degraded" if stale else "healthy",
                              "WORKER_HEARTBEAT_STALE" if stale else "OK",
                              "运行中作业可能停滞" if stale else "worker 心跳正常或当前空闲") | {"stale_jobs": stale}

    def _probe_queue(self) -> dict[str, Any]:
        _, jobs = self._project_modes_and_jobs()
        now = datetime.now(timezone.utc)
        stale = 0
        for job in jobs:
            if job.get("status") == "queued":
                try:
                    stale += (now - datetime.fromisoformat(job["created_at"])).total_seconds() > self.policy.queue_stale_seconds
                except (KeyError, TypeError, ValueError): stale += 1
        return self._external("queue", "degraded" if stale else "healthy", "QUEUE_BACKLOG_STALE" if stale else "OK",
                              "新作业可能延迟" if stale else "队列无超时滞留") | {"stale_count": stale}

    def _existing_storage_anchor(self) -> Path:
        anchor = self.projects_root
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        return anchor

    def _probe_storage(self) -> dict[str, Any]:
        anchor = self._existing_storage_anchor()
        free = os.statvfs(anchor).f_bavail * os.statvfs(anchor).f_frsize
        writable = os.access(anchor, os.W_OK | os.X_OK)
        if not writable:
            return self._external("storage", "unhealthy", "STORAGE_NOT_WRITABLE", "项目、作业和资产无法持久化") | {"anchor": str(anchor), "free_bytes": free}
        if free < self.policy.storage_min_free_bytes:
            return self._external("storage", "degraded", "STORAGE_CAPACITY_LOW", "新资产写入存在容量风险") | {"anchor": str(anchor), "free_bytes": free}
        return self._external("storage", "healthy", "OK", "存储可写且容量充足") | {"anchor": str(anchor), "free_bytes": free}

    def _probe_event_append(self) -> dict[str, Any]:
        anchor = self._existing_storage_anchor()
        writable = os.access(anchor, os.W_OK | os.X_OK)
        return self._external("event_append", "healthy" if writable else "unhealthy",
                              "OK" if writable else "EVENT_APPEND_UNAVAILABLE",
                              "业务事件可持久化" if writable else "状态变更无法安全记录") | {"anchor": str(anchor)}

    def _probe_asset_proxy(self) -> dict[str, Any]:
        configured = self.asset_proxy_configured()
        return self._external("asset_proxy", "healthy" if configured else "unhealthy",
                              "OK" if configured else "ASSET_PROXY_UNAVAILABLE",
                              "受控资产读取可用" if configured else "项目图片无法安全读取")

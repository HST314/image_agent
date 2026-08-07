"""Read-only P2-03 event and factual progress projections."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from storage.project_store import EventStore

MAX_EVENT_PAGE = 100
MAX_EVENT_SPAN = 10_000
_CURSOR_VERSION = 1
_TRACE = re.compile(r"^trace_[a-f0-9]{32}$")
_SAFE_STATUS = {"queued", "running", "succeeded", "failed", "cancel_requested", "cancelled",
                "waiting", "retrying", "recovered", "partial"}
_WAITING_PHASES = {"waiting_clarification", "waiting_task_spec_confirmation", "waiting_master_selection",
                   "waiting_human_approval", "waiting_quality_disposition", "waiting_human_rework",
                   "waiting_reinspection", "waiting_final_confirmation"}
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:api[_-]?key|token|secret|authorization)\s*[:=]\s*\S+|"
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|1[3-9]\d{9}|https?://\S+|(?:/[\w.-]+){2,})"
)


def _cursor(after: int, through: int) -> str:
    raw = json.dumps({"v": _CURSOR_VERSION, "after": after, "through": through},
                     sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(value: str) -> tuple[int, int]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
        if set(data) != {"v", "after", "through"} or data["v"] != _CURSOR_VERSION:
            raise ValueError
        after, through = int(data["after"]), int(data["through"])
        if after < 0 or through < after:
            raise ValueError
        return after, through
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("事件游标无效。") from exc


def _phase(event: dict[str, Any]) -> str:
    return str(event.get("state") or event.get("stage") or event.get("phase") or "system")[:128]


def _status(event: dict[str, Any]) -> str:
    explicit = str(event.get("status") or "")
    if explicit in _SAFE_STATUS:
        return explicit
    kind = str(event.get("type") or "")
    if kind.endswith(("_failed", "_invalidated")):
        return "failed"
    if kind.endswith(("_succeeded", "_finished", "_confirmed", "_completed")):
        return "succeeded"
    if kind.endswith(("_started", "_queued")):
        return "running" if kind.endswith("_started") else "queued"
    return "waiting" if _phase(event) in _WAITING_PHASES else "running"


def event_dto(event: dict[str, Any]) -> dict[str, Any]:
    """Allow-list the public shape; raw payloads and exception details never cross."""
    error = event.get("error") if isinstance(event.get("error"), dict) else {}
    trace = event.get("trace_id") or error.get("trace_id")
    return {
        "event_id": str(event.get("event_id") or ""),
        "sequence": int(event["sequence"]),
        "event_type": str(event.get("type") or "unknown")[:128],
        "phase": _phase(event),
        "job_id": str(event["job_id"])[:128] if event.get("job_id") else None,
        "slot": int(event.get("slot", event.get("index"))) if event.get("slot", event.get("index")) is not None else None,
        "round": int(event.get("round", event.get("rework_round"))) if event.get("round", event.get("rework_round")) is not None else None,
        "status": _status(event),
        "timestamp": str(event.get("timestamp") or ""),
        "trace_id": str(trace) if trace and _TRACE.fullmatch(str(trace)) else None,
        "retry_count": max(0, int(event.get("retry_count", event.get("attempt", 1))) - 1),
        "error_code": str(error.get("code"))[:128] if error.get("code") else None,
        "message": _SENSITIVE_TEXT.sub("[REDACTED]", str(event.get("marker")))[:256]
                   if event.get("marker") is not None else None,
    }


def event_page(events: EventStore, *, limit: int = 50, cursor: str | None = None,
               since: int | None = None) -> dict[str, Any]:
    if limit < 1 or limit > MAX_EVENT_PAGE:
        raise ValueError(f"事件分页大小必须在 1 到 {MAX_EVENT_PAGE} 之间。")
    if cursor and since is not None:
        raise ValueError("cursor 与 since 不能同时使用。")
    high = events.last_sequence()
    if cursor:
        after, through = _decode(cursor)
        if through > high or through - after > MAX_EVENT_SPAN:
            raise ValueError("事件游标超出允许查询范围。")
    else:
        after = int(since or 0)
        if after < 0 or after > high:
            raise ValueError("since 超出事件范围。")
        through = high
        if through - after > MAX_EVENT_SPAN:
            raise ValueError(f"事件查询跨度不能超过 {MAX_EVENT_SPAN}。")
    raw, _ = events.scan(after=after, through=through, limit=limit + 1)
    visible = raw[:limit]
    next_cursor = _cursor(int(visible[-1]["sequence"]), through) if len(raw) > limit else None
    return {"schema_version": 1, "items": [event_dto(item) for item in visible],
            "next_cursor": next_cursor, "through_sequence": through}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def progress_projection(project_root: Path, events: EventStore) -> dict[str, Any]:
    """Project only durable facts; this function performs no recovery or migration."""
    manifest = _read_json(project_root / "manifest.json", {})
    jobs_doc = _read_json(project_root / "runtime/jobs.json", {"jobs": {}})
    jobs = list((jobs_doc.get("jobs") or {}).values())
    jobs.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("job_id") or "")))
    job = jobs[-1] if jobs else None
    snapshot: dict[str, Any] = {}
    pointer = manifest.get("current_checkpoint") or {}
    if pointer.get("path"):
        envelope = _read_json(project_root / str(pointer["path"]), {})
        snapshot = envelope.get("data") or {}
    succeeded: set[int] = set()
    failed: set[int] = set()
    inspection_round = 0
    high = 0
    for raw in events.iter_readonly():
        item = event_dto(raw)
        high = item["sequence"]
        if item["event_type"] == "candidate_succeeded" and item["slot"] is not None:
            succeeded.add(item["slot"]); failed.discard(item["slot"])
        elif item["event_type"] == "candidate_failed" and item["slot"] is not None and item["slot"] not in succeeded:
            failed.add(item["slot"])
        if item["event_type"] == "inspection_presented":
            inspection_round = max(inspection_round, item["round"] or 0)
    succeeded_slots, failed_slots = sorted(succeeded), sorted(failed)
    policy = (_read_json(project_root / "project.yaml", {}).get("runtime_policy") or {}).get("policy") or {}
    max_rounds = int((policy.get("self_check") or {}).get("max_rounds", 1))
    inspection_round = max(inspection_round, int(snapshot.get("round", snapshot.get("quality_round", 0)) or 0))
    phase = str(snapshot.get("phase") or snapshot.get("state") or "received")
    status = "waiting" if phase in _WAITING_PHASES else str((job or {}).get("status") or "idle")
    if status == "failed" and phase in _WAITING_PHASES:
        status = "waiting"
    candidate_total = int(policy.get("candidate_count", 5))
    persisted = (job or {}).get("progress") or {"completed": len(succeeded_slots), "total": candidate_total, "unit": "candidate"}
    return {"schema_version": 1, "phase": phase, "status": status,
            "job": {"job_id": job.get("job_id"), "status": job.get("status"),
                    "attempt": int(job.get("attempt", 0)), "retry_count": max(0, int(job.get("attempt", 0)) - 1)} if job else None,
            "work": {"completed": int(persisted.get("completed", 0)), "total": int(persisted.get("total", 1)),
                     "unit": str(persisted.get("unit") or "workflow")},
            "candidates": {"completed_slots": succeeded_slots, "failed_slots": failed_slots, "total": candidate_total},
            "quality": {"current_round": inspection_round, "max_rounds": max_rounds,
                        "at_limit": bool(inspection_round >= max_rounds and snapshot.get("calibration_status") == "waiting_human_disposition")},
            "waiting_for_human": phase in _WAITING_PHASES, "through_sequence": high}

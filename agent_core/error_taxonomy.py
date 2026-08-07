"""Stable, sanitized error contract shared by jobs, checkpoints and HTTP adapters."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

Action = Literal["retry", "modify_input", "contact_admin", "human_decision", "none"]

_SECRET = re.compile(
    r"(?i)(authorization|api[-_ ]?key|token|secret|password)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+\-/=]+")


def sanitize_detail(value: object, *, limit: int = 512) -> str:
    """Keep actionable context without credentials or full provider bodies."""
    text = _BEARER.sub("Bearer [REDACTED]", str(value))
    text = _SECRET.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return text[:limit] + ("…" if len(text) > limit else "")


@dataclass(frozen=True)
class ClassifiedError:
    code: str
    retryable: bool
    suggested_action: Action
    http_status: int


def classify_exception(exc: BaseException) -> ClassifiedError:
    status = getattr(exc, "status_code", None)
    category = str(getattr(exc, "category", "")).lower()
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if name in {"cancellederror", "jobcancellederror"} or "cancel_requested" in message:
        return ClassifiedError("CANCELLED", False, "none", 409)
    if status == 429 or category == "rate_limited":
        return ClassifiedError("RATE_LIMITED", True, "retry", 429)
    if status == 401 or category == "authentication":
        return ClassifiedError("AUTHENTICATION_FAILED", False, "contact_admin", 401)
    if category == "content_policy" or (status == 403 and ("moder" in name or "policy" in message or "content" in message)):
        return ClassifiedError("CONTENT_REJECTED", False, "modify_input", 422)
    if category in {"validation_or_refusal", "request_rejected"} or status in {400, 404, 422} or isinstance(exc, (ValueError, TypeError)):
        return ClassifiedError("INVALID_INPUT", False, "modify_input", 422)
    if "skill" in name or "config" in name or category in {"configuration", "skill"}:
        return ClassifiedError("CONFIGURATION_OR_SKILL", False, "contact_admin", 503)
    if "asset" in name or category == "asset_ingestion":
        return ClassifiedError("ASSET_INGESTION_FAILED", True, "retry", 503)
    if "structured" in name or "json" in name or category == "structured_output":
        return ClassifiedError("STRUCTURED_OUTPUT_INVALID", True, "retry", 502)
    if isinstance(exc, TimeoutError) or category == "timeout" or "timeout" in name:
        return ClassifiedError("UPSTREAM_TIMEOUT", True, "retry", 504)
    if isinstance(exc, ConnectionError) or category in {"transport", "provider_unavailable", "provider_error"}:
        return ClassifiedError("PROVIDER_UNAVAILABLE", True, "retry", 503)
    if status and int(status) >= 500:
        return ClassifiedError("PROVIDER_UNAVAILABLE", True, "retry", 503)
    return ClassifiedError("INTERNAL_ERROR", False, "contact_admin", 500)


def retry_after_seconds(exc: BaseException) -> float | None:
    value: Any = getattr(exc, "retry_after", None)
    response = getattr(exc, "response", None)
    if value is None and response is not None:
        value = getattr(response, "headers", {}).get("Retry-After")
    if value is None:
        value = getattr(exc, "headers", {}).get("Retry-After") if hasattr(exc, "headers") else None
    try:
        result = float(value)
        return result if result >= 0 else None
    except (TypeError, ValueError):
        return None


def error_record(exc: BaseException, *, stage: str, slot: int | None = None,
                 rework_round: int | None = None, trace_id: str | None = None) -> dict[str, Any]:
    classified = classify_exception(exc)
    record: dict[str, Any] = {
        "code": classified.code, "stage": stage, "retryable": classified.retryable,
        "suggested_action": classified.suggested_action,
        "trace_id": trace_id or f"trace_{uuid4().hex}",
        "detail": sanitize_detail(exc),
    }
    if slot is not None:
        record["candidate_slot"] = slot
    if rework_round is not None:
        record["rework_round"] = rework_round
    after = retry_after_seconds(exc)
    if after is not None:
        record["retry_after_seconds"] = after
    return record

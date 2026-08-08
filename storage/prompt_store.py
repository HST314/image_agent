"""Append-only, concurrency-safe model-call audit records."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from storage.project_store import FORMAT_VERSION, ImmutableRecordError, content_hash, _now
from storage import file_lock


class CompletedProviderResultError(RuntimeError):
    """A paid provider result exists but cannot currently be ingested."""

    category = "asset_ingestion"


class PromptStore:
    """Durable model calls.  The historical name remains API-compatible."""

    REQUIRED = {"messages", "template_id", "template_version", "template_hash", "variables",
                "input_refs", "model", "parameters", "config_hash", "state", "trace_id"}
    SECRET_KEYS = re.compile(r"api.?key|authorization|cookie|credential|password|secret|token|signed.?url|restricted|local.?path|provider.?raw", re.I)
    SENSITIVE_VALUES = re.compile(
        r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|https?://[^\s]+(?:signature|sig|token|x-amz-signature)=[^\s&]+|"
        r"(?:[A-Za-z]:\\|/)(?:[^\s/\\]+[/\\]){2,}[^\s]*|restricted(?:_content)?\s*[:=]\s*[^,}\n]+)"
    )

    def __init__(self, path: Path) -> None:
        self.path = path if path.suffix == ".jsonl" else path / "prompts.jsonl"

    def begin(self, record: dict[str, Any]) -> str:
        missing = self.REQUIRED - record.keys()
        if missing:
            raise ValueError(f"Prompt 审计记录缺少必填项：{', '.join(sorted(missing))}")
        call_id = str(record.get("call_id") or record.get("prompt_id") or f"call_{uuid4().hex}")
        now = _now()
        capability = str(record.get("capability") or record.get("state"))
        model = record.get("model") or {}
        safe = self._redact(record)
        item = {"format_version": FORMAT_VERSION, "event_kind": "call_started", "call_id": call_id,
                "prompt_id": call_id, "sequence": 0, "created_at": now, "started_at": now,
                "status": "running", "capability": capability,
                "call_type": "image" if model.get("role") == "text_to_image_model" else "text",
                **safe}
        item["record_hash"] = content_hash(item)
        self._append_unique(item, unique=(call_id, "call_started", 0))
        return call_id

    def status(self, call_id: str, status: str, **details: Any) -> None:
        if status not in {"queued", "running", "provider_completed", "ingested"}:
            raise ValueError("模型调用状态无效。")
        self._append_event(call_id, "status", status=status, **details)

    def chunk(self, call_id: str, text: str) -> int:
        if not isinstance(text, str) or not text:
            raise ValueError("文本增量不能为空。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as stream:
            file_lock.lock(stream, file_lock.LOCK_EX)
            stream.seek(0)
            events = [json.loads(line) for line in stream if line.strip()]
            if not any(x.get("call_id") == call_id for x in events):
                raise FileNotFoundError(call_id)
            sequence = 1 + max((int(x.get("sequence", 0)) for x in events
                                if x.get("call_id") == call_id and x.get("event_kind") == "text_delta"), default=0)
            item = {"format_version": FORMAT_VERSION, "event_kind": "text_delta", "call_id": call_id,
                    "created_at": _now(), "sequence": sequence, "delta": self._redact(text)}
            item["record_hash"] = content_hash(item)
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush(); os.fsync(stream.fileno())
            file_lock.unlock(stream)
            return sequence

    def complete(self, prompt_id: str, *, output_raw: Any, output_parsed: Any = None,
                 output_ref: str | None = None) -> str:
        self.get(prompt_id)
        now = _now()
        started = datetime.fromisoformat(self.get(prompt_id)["started_at"])
        elapsed = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
        self._append_event(prompt_id, "call_finished", status="completed", completed_at=now,
                           duration_ms=elapsed, output_raw=self._redact(output_raw),
                           output_parsed=self._redact(output_parsed), output_ref=output_ref)
        return f"{prompt_id}.result"

    def fail(self, prompt_id: str, error: dict[str, Any]) -> str:
        self.get(prompt_id)
        self._append_event(prompt_id, "call_finished", status="failed", completed_at=_now(),
                           error=self._redact(error))
        return f"{prompt_id}.failed"

    def save(self, record: dict[str, Any]) -> str:
        return self.begin(record)

    def get(self, call_id: str) -> dict[str, Any]:
        events = self._events(call_id.removesuffix(".result").removesuffix(".failed"))
        if not events:
            raise FileNotFoundError(call_id)
        start = next((x for x in events if x.get("event_kind") == "call_started"), None)
        if start is None:
            raise ValueError("模型调用审计链缺少开始记录。")
        result = dict(start)
        deltas = []
        statuses = []
        for event in events[1:]:
            if event.get("event_kind") == "text_delta":
                deltas.append({"sequence": event["sequence"], "delta": event["delta"]})
            elif event.get("event_kind") == "status":
                statuses.append({"status": event["status"], "at": event["created_at"]})
            elif event.get("event_kind") == "call_finished":
                result.update({k: v for k, v in event.items() if k not in {"record_hash", "event_kind"}})
        result["text_deltas"] = deltas
        result["status_events"] = statuses
        return result

    def list_calls(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间。")
        offset = self._decode_cursor(cursor) if cursor else 0
        starts = [x for x in self._all() if x.get("event_kind") == "call_started"]
        starts.sort(key=lambda x: (x.get("created_at", ""), x["call_id"]), reverse=True)
        page = starts[offset:offset + limit]
        return {"items": [self.summary(x["call_id"]) for x in page],
                "next_cursor": self._encode_cursor(offset + limit) if offset + limit < len(starts) else None}

    def chunks(self, call_id: str, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 100 or after < 0:
            raise ValueError("增量游标或 limit 无效。")
        call = self.get(call_id)
        chunks = [x for x in call["text_deltas"] if x["sequence"] > after][:limit]
        final = call.get("output_raw") if call.get("status") == "completed" else None
        return {"call_id": call_id, "items": chunks, "next_after": chunks[-1]["sequence"] if chunks else after,
                "complete": call.get("status") == "completed", "final_response": final}

    def summary(self, call_id: str) -> dict[str, Any]:
        call = self.get(call_id)
        model = call.get("model") or {}
        return {k: call.get(k) for k in ("call_id", "capability", "call_type", "status", "started_at",
                                         "completed_at", "duration_ms", "config_hash", "parent_call_id", "trace_id")} | {
            "model": {"provider": model.get("provider"), "name": model.get("name"), "version": model.get("version")},
            "template": {"id": call.get("template_id"), "version": call.get("template_version")},
            "input_asset_hashes": self._asset_hashes(call.get("input_refs") or []),
            "prompt_summary": self._summarize(call.get("messages")),
            "result_summary": self._summarize(call.get("output_parsed") or call.get("output_raw")),
            "error": call.get("error"), "status_events": call["status_events"],
        }

    def detail(self, call_id: str) -> dict[str, Any]:
        return self._redact(self.get(call_id))

    def pending_provider_result(self, *, state: str, idempotency_key: str) -> tuple[str, dict[str, Any]] | None:
        """Return a completed image result that has not reached ingestion."""
        recovered = self.recoverable_provider_result(state=state, idempotency_key=idempotency_key)
        if recovered is None or recovered[1].get("artifact_id"):
            return None
        return recovered

    def recoverable_provider_result(self, *, state: str,
                                    idempotency_key: str) -> tuple[str, dict[str, Any]] | None:
        """Return a paid result needing ingestion or an ingested asset needing binding."""
        starts = [item for item in self._all()
                  if item.get("event_kind") == "call_started"
                  and item.get("state") == state
                  and (item.get("variables") or {}).get("idempotency_key") == idempotency_key]
        starts.sort(key=lambda item: (item.get("created_at", ""), item.get("call_id", "")), reverse=True)
        for start in starts:
            call_id = start["call_id"]
            call = self.get(call_id)
            status_events = [item for item in self._events(call_id) if item.get("event_kind") == "status"]
            statuses = {item["status"] for item in status_events}
            if "provider_completed" not in statuses:
                continue
            ingested = next((item for item in reversed(status_events) if item.get("status") == "ingested"), None)
            if ingested is not None:
                artifact_id, sha256 = ingested.get("artifact_id"), ingested.get("sha256")
                if not isinstance(artifact_id, str) or not isinstance(sha256, str):
                    raise CompletedProviderResultError("已入库供应商结果缺少稳定资产绑定，禁止再次付费。")
                return call_id, {"artifact_id": artifact_id, "uri": f"artifact://{artifact_id}",
                                 "sha256": sha256, "reference_hash": sha256,
                                 "provider": str((call.get("model") or {}).get("provider") or "unknown"),
                                 "model": str((call.get("model") or {}).get("name") or "unknown"),
                                 "mock": False}
            result = call.get("output_raw")
            if not isinstance(result, dict):
                raise CompletedProviderResultError("已完成供应商结果缺少可恢复的结构化响应，禁止再次付费。")
            uri = result.get("url") or result.get("uri")
            if not isinstance(uri, str) or not uri.startswith(("https://", "http://")):
                raise CompletedProviderResultError("已完成供应商结果的下载引用不可恢复，禁止再次付费。")
            return call_id, dict(result)
        return None

    def _append_event(self, call_id: str, kind: str, **payload: Any) -> None:
        if not self._events(call_id):
            raise FileNotFoundError(call_id)
        item = {"format_version": FORMAT_VERSION, "event_kind": kind, "call_id": call_id,
                "created_at": _now(), **payload}
        item["record_hash"] = content_hash(item)
        self._append_unique(item)

    def _events(self, call_id: str) -> list[dict[str, Any]]:
        return [x for x in self._all() if x.get("call_id") == call_id]

    def _all(self) -> list[dict[str, Any]]:
        if not self.path.exists(): return []
        with self.path.open("r", encoding="utf-8") as stream:
            file_lock.lock(stream, file_lock.LOCK_SH)
            try: return [json.loads(line) for line in stream if line.strip()]
            finally: file_lock.unlock(stream)

    def _append_unique(self, item: dict[str, Any], unique: tuple[Any, ...] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as stream:
            file_lock.lock(stream, file_lock.LOCK_EX)
            stream.seek(0)
            existing = [json.loads(line) for line in stream if line.strip()]
            if unique and any((x.get("call_id"), x.get("event_kind"), x.get("sequence")) == unique for x in existing):
                raise ImmutableRecordError("模型调用记录不可覆盖。")
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush(); os.fsync(stream.fileno())
            file_lock.unlock(stream)

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if hasattr(value, "model_dump"): value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {k: "[REDACTED]" if cls.SECRET_KEYS.search(str(k)) else cls._redact(v) for k, v in value.items()}
        if isinstance(value, list): return [cls._redact(v) for v in value]
        if isinstance(value, str): return cls.SENSITIVE_VALUES.sub("[REDACTED]", value)
        return value

    @staticmethod
    def _asset_hashes(refs: list[Any]) -> list[str]:
        hashes = []
        for ref in refs:
            match = re.search(r"(?:sha256:|artifact_)([a-f0-9]{64})", str(ref))
            if match: hashes.append(match.group(1))
        return hashes

    @classmethod
    def _summarize(cls, value: Any) -> dict[str, Any] | None:
        if value is None: return None
        safe = json.dumps(cls._redact(value), ensure_ascii=False, sort_keys=True)
        return {"sha256": hashlib.sha256(safe.encode()).hexdigest(), "characters": len(safe)}

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(f"model-calls-v1:{offset}".encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> int:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
            prefix, value = raw.split(":", 1)
            if prefix != "model-calls-v1" or int(value) < 0: raise ValueError
            return int(value)
        except Exception as exc:
            raise ValueError("模型调用游标无效或版本不兼容。") from exc

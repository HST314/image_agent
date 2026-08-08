"""Versioned, immutable P1-09 delivery generation and manual return records."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from storage.project_store import ProjectStore, atomic_json, content_hash


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_note(sources: dict[str, Any]) -> dict[str, str]:
    style = sources["style"]
    task = sources["task"]
    return {
        "design_concept": str(style["artistic_philosophy"]),
        "selection_reason": str(style["fit_reason"]),
        "task_fit": f"围绕{task['deliverable_goal']}，适配{task['usage_context']}；最终图已通过质检。",
    }


class DeliveryService:
    """Builds deliveries outside checkpoints so note retries cannot touch frozen state."""

    def __init__(self, store: ProjectStore, *, note_generator: Callable[[dict[str, Any]], dict[str, str]] | None = None) -> None:
        self.store = store
        self.note_generator = note_generator or _default_note
        self.root = store.root / "deliveries"

    def _version_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(self.root.glob("delivery-*.json"))

    def list_versions(self) -> list[dict[str, Any]]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in self._version_paths()]

    def _validated_sources(self, state: dict[str, Any]) -> dict[str, Any]:
        if not state.get("delivery_frozen") or not state.get("final_asset"):
            raise ValueError("最终资产尚未确认并冻结，禁止生成 Delivery。")
        spec = state.get("task_specification") or {}
        spec_confirmation = state.get("task_spec_confirmation") or {}
        if (spec_confirmation.get("task_spec_version") != spec.get("version")
                or spec_confirmation.get("subject_sha256") != spec.get("content_hash")):
            raise ValueError("任务书确认与当前任务书版本不一致。")
        asset = state["final_asset"]
        confirmation = state.get("final_confirmation") or {}
        if confirmation.get("asset_sha256") != asset.get("sha256"):
            raise ValueError("最终确认绑定的资产哈希与最终资产不一致。")
        if state.get("latest_checked_asset_hash") != asset.get("sha256"):
            raise ValueError("质检事实未绑定最终资产哈希。")
        if not confirmation.get("actor") or not confirmation.get("confirmed_at"):
            raise ValueError("最终确认事实不完整。")
        artifact_id = str(asset.get("artifact_id") or "")
        path = self.store.artifacts.resolve(artifact_id)
        record = self.store.artifacts.record(artifact_id)
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha != asset.get("sha256") or record.get("sha256") != actual_sha:
            raise ValueError("最终资产真实内容哈希校验失败。")
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            image_format = str(image.format or "").lower()
        media_type = str(record.get("media_type") or f"image/{image_format}")
        master = state.get("master_asset") or {}
        style_index = master.get("style_index") or (master.get("style_slot_audit") or {}).get("style_index")
        cards = state.get("style_idea_cards") or []
        style = next((card for card in cards if card.get("style_index") == style_index), None)
        if style is None or not all(str(style.get(k) or "").strip() for k in ("fit_reason", "artistic_philosophy")):
            raise ValueError("最终采用风格及选择理由不可追溯。")
        task = state.get("task_card") or {}
        if not task.get("task_id") or not task.get("deliverable_goal") or not task.get("usage_context"):
            raise ValueError("已确认任务书来源不完整。")
        history = self.store.history()
        task_trace = next((event for event in reversed(history) if event.get("type") == "task_spec_confirmed"
                           and event.get("subject_sha256") == spec.get("content_hash")), None)
        quality_trace = next((event for event in reversed(history) if event.get("type") == "inspection_completed"
                              and (event.get("input_asset") or {}).get("sha256") == actual_sha), None)
        final_trace = next((event for event in reversed(history) if event.get("type") == "final_asset_confirmed"
                            and event.get("asset_sha256") == actual_sha), None)
        if not task_trace or not quality_trace or not final_trace:
            raise ValueError("Delivery 所需确认与质检 trace 不完整。")
        trace_refs = [task_trace["event_id"], quality_trace["event_id"], final_trace["event_id"]]
        return {
            "task": {"task_id": task["task_id"], "deliverable_goal": task["deliverable_goal"],
                     "usage_context": task["usage_context"], "spec_version": spec["version"],
                     "spec_sha256": spec["content_hash"]},
            "style": {"style_index": style_index, "title": style.get("title"),
                      "fit_reason": style["fit_reason"], "artistic_philosophy": style["artistic_philosophy"]},
            "quality": {"asset_sha256": actual_sha, "inspection": state.get("inspection"),
                        "latest_checked_asset_hash": state.get("latest_checked_asset_hash")},
            "asset": {"artifact_id": artifact_id, "uri": record["uri"], "sha256": actual_sha,
                      "format": image_format, "media_type": media_type, "width": width, "height": height,
                      "size_bytes": path.stat().st_size},
            "task_confirmation": spec_confirmation,
            "final_confirmation": confirmation,
            "trace_refs": trace_refs,
        }

    def generate(self, state: dict[str, Any]) -> dict[str, Any]:
        with self.store.lock():
            sources = self._validated_sources(state)
            source_sha = content_hash(sources)
            try:
                note = self.note_generator(sources)
                required = ("design_concept", "selection_reason", "task_fit")
                if not isinstance(note, dict) or not all(str(note.get(key) or "").strip() for key in required):
                    raise ValueError("设计说明必须包含设计理念、选择理由和任务适配点。")
            except Exception as exc:
                self.store.events.append("delivery_note_generation_failed", source_sha256=source_sha,
                                         retryable=True, error_type=type(exc).__name__)
                raise
            design_note = (f"设计理念：{note['design_concept']}\n"
                           f"选择理由：{note['selection_reason']}\n"
                           f"任务适配点：{note['task_fit']}")
            payload_sha = content_hash({"sources": sources, "design_note": design_note})
            existing = self.list_versions()
            matched = next((item for item in existing if item.get("payload_sha256") == payload_sha), None)
            if matched:
                return self._with_return(matched)
            version = max((int(item["delivery_version"]) for item in existing), default=0) + 1
            delivery = {
                "schema_version": "1.1", "delivery_version": version,
                "task_id": sources["task"]["task_id"], "design_job_id": self.store.project_id,
                "status": "ready", "return_status": "pending_return",
                "final_image": sources["asset"], "design_note": design_note,
                "design_note_sources": {"task": sources["task"], "style": sources["style"],
                                        "quality_sha256": content_hash(sources["quality"])},
                "task_confirmation": sources["task_confirmation"],
                "final_confirmation": sources["final_confirmation"],
                "trace_refs": sources["trace_refs"], "source_sha256": source_sha,
                "payload_sha256": payload_sha, "created_at": _now(),
            }
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_json(self.root / f"delivery-{version:06d}.json", delivery)
            self.store.events.append("delivery_generated", delivery_version=version,
                                     payload_sha256=payload_sha, source_sha256=source_sha)
            return delivery

    def _return_records(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.root.glob("return-*.json"))]

    def _with_return(self, delivery: dict[str, Any]) -> dict[str, Any]:
        record = next((item for item in self._return_records()
                       if item["delivery_version"] == delivery["delivery_version"]), None)
        return {**delivery, "return_status": "returned", "return_record": record} if record else delivery

    def get(self, version: int | None = None) -> dict[str, Any]:
        deliveries = self.list_versions()
        if not deliveries:
            raise ValueError("设计说明尚未生成，当前没有可读取的 Delivery。")
        delivery = deliveries[-1] if version is None else next(
            (item for item in deliveries if item["delivery_version"] == version), None)
        if delivery is None:
            raise ValueError("Delivery 版本不存在。")
        return self._with_return(delivery)

    def record_return(self, version: int, *, actor: str, target: str, idempotency_key: str) -> dict[str, Any]:
        with self.store.lock():
            if not actor.strip() or not target.strip() or not idempotency_key.strip():
                raise ValueError("人工回传必须提供 actor、目标和幂等键。")
            delivery = self.get(version)
            payload = {"delivery_version": version, "actor": actor.strip(), "target": target.strip()}
            payload_sha = content_hash(payload)
            records = self._return_records()
            same_key = next((item for item in records if item["idempotency_key"] == idempotency_key), None)
            if same_key and same_key["payload_sha256"] != payload_sha:
                raise ValueError("同一回传幂等键不能用于不同载荷。")
            if same_key:
                return same_key
            same_version = next((item for item in records if item["delivery_version"] == version), None)
            if same_version:
                return same_version
            record = {**payload, "idempotency_key": idempotency_key, "payload_sha256": payload_sha,
                      "delivery_payload_sha256": delivery["payload_sha256"], "returned_at": _now()}
            atomic_json(self.root / f"return-{version:06d}.json", record)
            self.store.events.append("delivery_returned", **record)
            return record

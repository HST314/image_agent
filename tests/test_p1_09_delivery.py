from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import pytest
import jsonschema
from PIL import Image

from agent_core.delivery import DeliveryService
from agent_core.models import DesignDeliveryEnvelope
from storage.project_store import ProjectStore


def _frozen(tmp_path: Path, project: str = "delivery") -> tuple[ProjectStore, dict]:
    store = ProjectStore(tmp_path, project); store.create()
    image = Image.new("RGB", (37, 19), "#123456")
    stream = io.BytesIO(); image.save(stream, format="PNG")
    asset = store.artifacts.save_bytes(stream.getvalue(), suffix=".png", metadata={
        "media_type": "image/png", "width": 37, "height": 19,
        "project_id": project, "branch": "main",
    })
    spec = {"task_id": "task-1", "version": 2,
            "facts": [{"label": "目标", "value": "夏季新品主视觉"}],
            "parent_hash": "old", "content_hash": "spec-hash"}
    state = {
        "state": "final_approval", "phase": "delivery_frozen", "delivery_frozen": True,
        "task_card": {"task_id": "task-1", "project_id": project,
                      "source_refs": [{"ref_id": "brief-1", "ref_type": "brief"}],
                      "deliverable_goal": "夏季新品主视觉", "usage_context": "社交媒体投放",
                      "known_facts": {}, "unknowns": {}, "asset_inputs": [], "status": "draft"},
        "task_specification": spec,
        "task_spec_confirmation": {"task_spec_version": 2, "subject_sha256": "spec-hash",
                                   "actor": "planner", "confirmed_at": "2026-08-07T10:00:00Z"},
        "style_idea_cards": [{"style_index": 3, "title": "清透夏日", "fit_reason": "突出清爽与新品感",
                              "artistic_philosophy": "用留白和冷暖对比聚焦新品"}],
        "master_asset": {**asset, "id": "candidate-3", "style_slot_audit": {"style_index": 3}},
        "final_asset": asset,
        "inspection": {"passed": True, "decision": "pass", "deviations": [], "confidence": .98},
        "latest_checked_asset_hash": asset["sha256"], "termination_satisfied": True,
        "final_confirmation": {"asset_sha256": asset["sha256"], "actor": "reviewer",
                               "confirmed_at": "2026-08-07T11:00:00Z"},
    }
    store.events.append("task_spec_confirmed", **state["task_spec_confirmation"])
    store.events.append("inspection_completed", round=1, input_asset=asset, result=state["inspection"])
    store.events.append("final_asset_confirmed", **state["final_confirmation"])
    store.checkpoint("final_approval", state)
    return store, state


def test_generation_requires_all_confirmation_gates(tmp_path: Path):
    store, state = _frozen(tmp_path)
    service = DeliveryService(store)
    for mutation in ("delivery_frozen", "final_confirmation", "task_spec_confirmation"):
        invalid = copy.deepcopy(state)
        invalid[mutation] = False if mutation == "delivery_frozen" else None
        with pytest.raises(ValueError, match="确认|冻结"):
            service.generate(invalid)
    invalid = copy.deepcopy(state); invalid["final_confirmation"]["asset_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="哈希"):
        service.generate(invalid)


def test_delivery_is_complete_traceable_stable_and_checkpoint_independent(tmp_path: Path):
    store, state = _frozen(tmp_path)
    delivery = DeliveryService(store).generate(state)
    DesignDeliveryEnvelope.model_validate(delivery)
    schema = json.loads(Path("schemas/DesignDeliveryEnvelope.v1.schema.json").read_text())
    jsonschema.validate(delivery, schema)
    assert delivery["delivery_version"] == 1 and delivery["return_status"] == "pending_return"
    assert delivery["task_id"] == "task-1" and delivery["design_job_id"] == store.project_id
    assert delivery["final_image"]["format"] == "png"
    assert delivery["final_image"]["width"] == 37 and delivery["final_image"]["height"] == 19
    assert delivery["final_image"]["uri"].startswith("artifact://")
    assert not any(token in str(delivery) for token in ("http://", "https://", "/tmp/", str(tmp_path)))
    assert delivery["final_confirmation"]["asset_sha256"] == delivery["final_image"]["sha256"]
    assert len(delivery["trace_refs"]) >= 3
    assert all(key in delivery["design_note"] for key in ("设计理念", "选择理由", "任务适配点"))
    assert "夏季新品" in delivery["design_note"] and "清爽" in delivery["design_note"]
    checkpoint = store.root / store.manifest()["current_checkpoint"]["path"]
    checkpoint.rename(checkpoint.with_suffix(".unavailable"))
    assert DeliveryService(store).get() == delivery


def test_generation_failure_is_retriable_and_never_mutates_frozen_bytes_or_confirmation(tmp_path: Path):
    store, state = _frozen(tmp_path)
    before_bytes = store.artifacts.resolve(state["final_asset"]["artifact_id"]).read_bytes()
    before_confirmation = copy.deepcopy(state["final_confirmation"])
    service = DeliveryService(store, note_generator=lambda _sources: (_ for _ in ()).throw(RuntimeError("model down")))
    with pytest.raises(RuntimeError, match="model down"):
        service.generate(state)
    assert store.artifacts.resolve(state["final_asset"]["artifact_id"]).read_bytes() == before_bytes
    assert state["final_confirmation"] == before_confirmation
    assert service.list_versions() == []
    delivery = DeliveryService(store).generate(state)
    assert delivery["delivery_version"] == 1


def test_repeat_generate_and_return_are_idempotent_but_changed_note_versions(tmp_path: Path):
    store, state = _frozen(tmp_path)
    service = DeliveryService(store)
    first = service.generate(state)
    assert service.generate(state) == first and len(service.list_versions()) == 1
    returned = service.record_return(first["delivery_version"], actor="operator", target="parent-agent",
                                     idempotency_key="return-001")
    assert service.record_return(1, actor="operator", target="parent-agent",
                                 idempotency_key="return-001") == returned
    assert service.record_return(1, actor="someone-else", target="other",
                                 idempotency_key="return-002") == returned
    assert len([e for e in store.history() if e["type"] == "delivery_returned"]) == 1
    changed = DeliveryService(store, note_generator=lambda _: {
        "design_concept": "仍只引用已确认事实", "selection_reason": "突出清爽与新品感",
        "task_fit": "适配夏季新品主视觉和社交媒体投放",
    }).generate(state)
    assert changed["delivery_version"] == 2 and changed["return_status"] == "pending_return"
    assert first["return_status"] == "pending_return"


def test_asset_change_cannot_reuse_old_confirmation(tmp_path: Path):
    store, state = _frozen(tmp_path)
    DeliveryService(store).generate(state)
    changed = copy.deepcopy(state)
    changed["final_asset"] = {**state["final_asset"], "sha256": "f" * 64}
    with pytest.raises(ValueError, match="哈希"):
        DeliveryService(store).generate(changed)


def test_same_return_key_with_different_payload_conflicts(tmp_path: Path):
    store, state = _frozen(tmp_path)
    DeliveryService(store).generate(state)
    service = DeliveryService(store)
    service.record_return(1, actor="operator", target="parent-agent", idempotency_key="return-001")
    with pytest.raises(ValueError, match="幂等键"):
        service.record_return(1, actor="operator", target="other", idempotency_key="return-001")

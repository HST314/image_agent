from __future__ import annotations

import json
from pathlib import Path

import pytest
import jsonschema
from pydantic import ValidationError

from agent_core.models import DesignDeliveryEnvelope, DesignTaskEnvelope
from agent_core.workflow_runner import RunnerOptions, SkillLoadError, WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from storage.project_store import ProjectStore, content_hash


def _json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_contract_fixtures_and_immutable_raw_input() -> None:
    task_valid = _json("examples/design_task_envelope_v1.valid.json")
    delivery_valid = _json("examples/design_delivery_envelope_v1.valid.json")
    resolver = jsonschema.RefResolver(Path("schemas/DesignTaskEnvelope.v1.schema.json").resolve().as_uri(),
                                      _json("schemas/DesignTaskEnvelope.v1.schema.json"))
    jsonschema.validate(task_valid, _json("schemas/DesignTaskEnvelope.v1.schema.json"), resolver=resolver)
    jsonschema.validate(delivery_valid, _json("schemas/DesignDeliveryEnvelope.v1.schema.json"))
    task = DesignTaskEnvelope.model_validate(task_valid)
    DesignDeliveryEnvelope.model_validate(delivery_valid)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_json("examples/design_delivery_envelope_v1.invalid.json"), _json("schemas/DesignDeliveryEnvelope.v1.schema.json"))
    with pytest.raises(ValidationError):
        DesignTaskEnvelope.model_validate(_json("examples/design_task_envelope_v1.invalid.json"))
    with pytest.raises(ValidationError):
        DesignDeliveryEnvelope.model_validate(_json("examples/design_delivery_envelope_v1.invalid.json"))
    with pytest.raises(ValidationError):
        task.idempotency_key = "changed"


def test_design_task_idempotency_claim_reuses_and_rejects_mutation(tmp_path: Path) -> None:
    raw = _json("examples/design_task_envelope_v1.valid.json")
    digest = content_hash(raw)
    assert ProjectStore.claim_design_task(tmp_path, "p1", raw["idempotency_key"], digest) == ("p1", True)
    assert ProjectStore.claim_design_task(tmp_path, "p2", raw["idempotency_key"], digest) == ("p1", False)
    with pytest.raises(ValueError, match="不同的原始任务"):
        ProjectStore.claim_design_task(tmp_path, "p3", raw["idempotency_key"], "0" * 64)


def test_required_skill_failure_blocks_before_render_and_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ProjectStore(tmp_path, "p"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    monkeypatch.setattr("skills.style_loader.StyleCardLoader.select_distinct", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing")))
    rendered = []
    runner._image_call = lambda *_args, **_kwargs: rendered.append(True)
    data = {
        "task_card": _json("examples/design_task_envelope_v1.valid.json")["task"],
        "task_specification": {"task_id": "task-contract-example", "version": 1, "facts": [], "parent_hash": None, "content_hash": "s"},
        "task_spec_confirmation": {"task_spec_version": 1, "subject_sha256": "s", "actor": "a", "confirmed_at": "now"},
    }
    with pytest.raises(SkillLoadError):
        runner.run(data, RunnerOptions(), only_state="initial_candidate_generation")
    assert not rendered
    assert any(e["type"] == "skill_load_blocked" and e["skill"] == "style_library" for e in store.history())


def test_explicit_degraded_policy_is_visible(tmp_path: Path) -> None:
    policy = RuntimePolicy.from_file(Path("configs/runtime.yaml")).model_copy(update={"skill_failure_mode": "allow_degraded"})
    store = ProjectStore(tmp_path, "p"); store.create({"runtime_policy": policy.snapshot("offline")})
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True, runtime_policy=policy)
    reasons = []
    runner._handle_skill_failure("style_library", OSError("missing"), reasons)
    assert reasons and any(e["type"] == "skill_degraded" and e["visible"] for e in store.history())

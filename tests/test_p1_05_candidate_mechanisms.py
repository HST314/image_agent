from pathlib import Path

import pytest

from agent_core.models import StyleIdeaCard
from agent_core.workflow_runner import validate_candidate_mechanisms
from agent_core.batch import CandidateBatchGenerator
from storage.project_store import ProjectStore, content_hash


def _idea(index: int) -> StyleIdeaCard:
    return StyleIdeaCard(
        task_id="p1-05", source_style_id=f"style-{index}", style_index=f"STYLE-{index:03d}",
        style_summary="摘要", title=f"方向 {index}", composition=f"构图 {index}",
        material=f"材质 {index}", lighting=f"光影 {index}", narrative=f"叙事 {index}",
        graphic_language=f"图形语言 {index}", fit_reason="适配", artistic_philosophy="理念",
        adaptable_mechanism="机制", prohibited_copy_elements=["主体"], major_risk="风险",
        prompt_supplement="补充",
    )


def test_five_mechanisms_require_unique_identity_complete_distinct_signatures():
    ideas = [_idea(index) for index in range(5)]
    validate_candidate_mechanisms(ideas)

    duplicate_index = [*ideas[:-1], ideas[-1].model_copy(update={"style_index": ideas[0].style_index})]
    with pytest.raises(ValueError, match="style_index"):
        validate_candidate_mechanisms(duplicate_index)

    duplicate_mechanism = [*ideas[:-1], ideas[-1].model_copy(update={
        field: getattr(ideas[0], field)
        for field in ("composition", "material", "lighting", "narrative", "graphic_language")
    })]
    with pytest.raises(ValueError, match="机制重复"):
        validate_candidate_mechanisms(duplicate_mechanism)


def test_slot_identity_is_bound_to_stable_key_and_retry_only_renders_failed_slot(tmp_path: Path):
    store = ProjectStore(tmp_path, "p1-05-slots")
    store.create()
    calls: list[int] = []
    fail_once = {3}

    def render(index: int):
        calls.append(index)
        if index in fail_once:
            fail_once.remove(index)
            raise RuntimeError("provider timeout")
        return {"uri": str(index), "sha256": str(index), "candidate_index": index}

    identities = [f"STYLE-{index:03d}" for index in range(5)]
    generator = CandidateBatchGenerator(store, render, attempts=1)
    first = generator.generate("spec-hash", slot_identities=identities)
    second = generator.generate("spec-hash", slot_identities=identities)
    assert [failure["index"] for failure in first["failed"]] == [3]
    assert not second["failed"] and len(second["succeeded"]) == 5
    assert calls.count(3) == 2 and all(calls.count(index) == 1 for index in (0, 1, 2, 4))
    events = [event for event in store.history() if event.get("index") == 3]
    assert len({event["idempotency_key"] for event in events}) == 1
    assert events[0]["idempotency_key"] == content_hash(
        ["initial_candidate_generation", "spec-hash", 3, "STYLE-003"]
    )

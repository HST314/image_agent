import json
from pathlib import Path

import pytest

from agent_core.models import ImageTaskCard, SourceRef, TaskConfirmationDoc
from agent_core.workflow_runner import RunnerOptions, SkillLoadError, WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from skills.style_idea_generator import StyleIdeaGenerator
from skills.style_loader import StyleCardLoader
from storage.project_store import ProjectStore


STYLE_INDEX = Path("skills/style_cards/index.json")
RELEVANT_TASK_TEXT = "编排网格 主视觉候选 编辑式动线 上下文预览 极简信号"


def task(goal: str = "用于决策审核的结构化对比海报") -> ImageTaskCard:
    return ImageTaskCard(
        task_id="p1-03",
        project_id="style-project",
        source_refs=[SourceRef(ref_id="source", ref_type="text")],
        deliverable_goal=goal,
        usage_context="手机展示与审核",
        known_facts={"主体": "产品"},
    )


def test_style_skill_selects_five_indexed_distinct_entries_with_complete_explanations():
    cards = StyleCardLoader(STYLE_INDEX).select_distinct(count=5, task_text=RELEVANT_TASK_TEXT)
    assert len(cards) == 5
    assert len({card.style_index for card in cards}) == 5
    assert all(card.reference_image.sha256 for card in cards)
    assert [card.style_index for card in cards] == [
        card.style_index
        for card in StyleCardLoader(STYLE_INDEX).select_distinct(
            count=5, task_text=RELEVANT_TASK_TEXT
        )
    ]

    ideas = StyleIdeaGenerator(offline_mode=True).generate(
        task_card=task(),
        confirmation_doc=TaskConfirmationDoc(
            task_id="p1-03", confirmed_facts=[], default_handling_for_unknowns=[]
        ),
        style_cards=cards,
    )
    assert len(ideas) == 5
    assert {idea.source_style_id for idea in ideas} == {card.style_id for card in cards}
    assert all(
        idea.fit_reason and idea.artistic_philosophy and idea.adaptable_mechanism and idea.major_risk
        for idea in ideas
    )


def _isolated_library(tmp_path: Path) -> tuple[Path, dict]:
    source_card = json.loads(Path("skills/style_cards/style_composed_grid.json").read_text(encoding="utf-8"))
    (tmp_path / "references").mkdir()
    source_ref = Path("skills/style_cards/references/composed_grid.svg")
    (tmp_path / "references/composed_grid.svg").write_bytes(source_ref.read_bytes())
    (tmp_path / "card.json").write_text(json.dumps(source_card, ensure_ascii=False), encoding="utf-8")
    index = {"items": [{
        "style_id": source_card["style_id"], "style_index": source_card["style_index"],
        "path": "card.json", "priority": 1,
    }]}
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return index_path, source_card


def test_style_skill_rejects_reference_hash_mismatch(tmp_path: Path):
    index_path, _ = _isolated_library(tmp_path)
    (tmp_path / "references/composed_grid.svg").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        StyleCardLoader(index_path).select_distinct(count=1)


def test_style_skill_rejects_duplicate_style_index(tmp_path: Path):
    index_path, card = _isolated_library(tmp_path)
    duplicate = dict(card)
    duplicate["style_id"] = "duplicate"
    (tmp_path / "duplicate.json").write_text(json.dumps(duplicate, ensure_ascii=False), encoding="utf-8")
    index_path.write_text(json.dumps({"items": [
        {"style_id": card["style_id"], "style_index": card["style_index"], "path": "card.json"},
        {"style_id": "duplicate", "style_index": card["style_index"], "path": "duplicate.json"},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate style_index"):
        StyleCardLoader(index_path).select_distinct(count=1)


@pytest.mark.parametrize("missing", ["summary", "best_for", "avoid_for", "risk_notes", "version"])
def test_style_skill_rejects_required_catalog_fields(tmp_path: Path, missing: str):
    index_path, card = _isolated_library(tmp_path)
    card.pop(missing)
    (tmp_path / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        StyleCardLoader(index_path).select_distinct(count=1)


def test_style_skill_rejects_fewer_than_five_and_prohibited_match(tmp_path: Path):
    with pytest.raises(ValueError, match="does not contain 6"):
        StyleCardLoader(STYLE_INDEX).select_distinct(count=6, task_text="结构化审核")
    with pytest.raises(ValueError, match="does not contain 5"):
        StyleCardLoader(STYLE_INDEX).select_distinct(count=5, task_text="要求高密度信息审核")


def test_style_skill_rejects_unique_style_index_drift(tmp_path: Path):
    index_path, card = _isolated_library(tmp_path)
    card["style_index"] = "STYLE-999"
    (tmp_path / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Style index identity mismatch"):
        StyleCardLoader(index_path).select_distinct(count=1, task_text="编排网格")


def test_style_skill_rejects_zero_relevance_and_fewer_than_five_matches():
    with pytest.raises(ValueError, match="does not contain 5"):
        StyleCardLoader(STYLE_INDEX).select_distinct(
            count=5, task_text="量子发动机 深海钻井 宠物医疗"
        )
    with pytest.raises(ValueError, match="does not contain 5"):
        StyleCardLoader(STYLE_INDEX).select_distinct(count=5, task_text="编排网格")


def test_historical_project_resolves_recorded_style_version_after_current_changes(tmp_path: Path):
    index_path, card = _isolated_library(tmp_path)
    historical = tmp_path / "history" / "style-001-v1.json"
    historical.parent.mkdir()
    historical.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    current = dict(card); current["version"] = "2.0"; current["summary"] = "new current"
    (tmp_path / "card.json").write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    index_path.write_text(json.dumps({"items": [{
        "style_id": card["style_id"], "style_index": card["style_index"], "version": "2.0",
        "path": "card.json", "versions": {"1.0": "history/style-001-v1.json", "2.0": "card.json"}
    }]}), encoding="utf-8")
    resolved = StyleCardLoader(index_path).load_version(card["style_index"], "1.0")
    assert resolved.version == "1.0" and resolved.summary == card["summary"]


def test_generator_rejects_duplicate_selection_before_model_call():
    cards = StyleCardLoader(STYLE_INDEX).select_distinct(count=5, task_text=RELEVANT_TASK_TEXT)

    class PaidClient:
        def __init__(self):
            self.calls = 0

        def inspect(self, *_args):
            self.calls += 1
            return {}

    client = PaidClient()
    with pytest.raises(ValueError, match="duplicate"):
        StyleIdeaGenerator(client=client).generate(
            task_card=task(),
            confirmation_doc=TaskConfirmationDoc(
                task_id="p1-03", confirmed_facts=[], default_handling_for_unknowns=[]
            ),
            style_cards=[cards[0], cards[0], *cards[2:]],
        )
    assert client.calls == 0


def test_zero_relevance_blocks_render_even_when_other_skills_allow_degraded(tmp_path: Path):
    policy = RuntimePolicy.from_file(Path("configs/runtime.yaml")).model_copy(
        update={"skill_failure_mode": "allow_degraded"}
    )
    store = ProjectStore(tmp_path, "p1-03-prepay")
    store.create({"runtime_policy": policy.snapshot("offline")})
    runner = WorkflowRunner(
        store, Path("configs/model_config.yaml"), offline_mode=True, runtime_policy=policy
    )
    paid_calls = []
    runner._image_call = lambda *_args, **_kwargs: paid_calls.append(True)
    envelope = json.loads(Path("examples/design_task_envelope_v1.valid.json").read_text(encoding="utf-8"))
    data = {
        "task_card": envelope["task"],
        "task_specification": {
            "task_id": envelope["task"]["task_id"], "version": 1, "facts": [],
            "parent_hash": None, "content_hash": "confirmed-hash",
        },
        "task_spec_confirmation": {
            "task_spec_version": 1, "subject_sha256": "confirmed-hash",
            "actor": "reviewer", "confirmed_at": "now",
        },
    }
    with pytest.raises(SkillLoadError, match="style_library"):
        runner.run(data, RunnerOptions(), only_state="initial_candidate_generation")
    assert paid_calls == []
    assert any(event["type"] == "skill_load_blocked" for event in store.history())

from pathlib import Path

import json
import shutil
import subprocess

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
    with pytest.raises(ValueError, match="机制差异不足"):
        validate_candidate_mechanisms(duplicate_mechanism)

    one_dimension_only = [ideas[0].model_copy(update={"style_index": f"STYLE-{index:03d}",
                           "graphic_language": f"图形语言 {index}"}) for index in range(5)]
    with pytest.raises(ValueError, match="至少 3 维"):
        validate_candidate_mechanisms(one_dimension_only)


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


def test_two_failed_slots_resume_with_complete_persisted_audit(tmp_path: Path):
    store = ProjectStore(tmp_path, "p1-05-two-slots"); store.create()
    calls, fail_once = [], {1, 4}
    def render(index: int):
        calls.append(index)
        if index in fail_once:
            fail_once.remove(index); raise RuntimeError("timeout")
        audit = {"slot": index, "style_index": f"STYLE-{index:03d}", "prompt_sha256": str(index),
                 "render_idempotency_key": f"render-{index}"}
        return {"uri": str(index), "sha256": str(index), "candidate_index": index, "style_slot_audit": audit}
    generator = CandidateBatchGenerator(store, render, attempts=1)
    first = generator.generate("spec", slot_identities=[f"STYLE-{i:03d}" for i in range(5)])
    second = generator.generate("spec", slot_identities=[f"STYLE-{i:03d}" for i in range(5)])
    assert [x["index"] for x in first["failed"]] == [1, 4]
    assert calls == [0, 1, 2, 3, 4, 1, 4]
    assert {x["style_slot_audit"]["slot"] for x in second["succeeded"]} == set(range(5))


# ---- P1-05 UI：waiting_candidate_retry 状态—动作契约（DOM 与交互负向测试） ----

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_HTML = REPO_ROOT / "frontend" / "index.html"
UI_HARNESS = REPO_ROOT / "frontend_tests" / "candidate_retry_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "candidate_retry_ui_driver.js"


def _extract_js_function(html: str, name: str) -> str:
    """按花括号配对提取页面内联脚本中指定函数的完整源码。"""
    start = html.index(f"function {name}(")
    brace = html.index("{", start)
    depth = 0
    for pos in range(brace, len(html)):
        if html[pos] == "{":
            depth += 1
        elif html[pos] == "}":
            depth -= 1
            if depth == 0:
                return html[start:pos + 1]
    raise AssertionError(f"函数 {name} 花括号未闭合")


def test_candidate_retry_phase_routes_to_dedicated_retry_view() -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert "s.phase==='waiting_candidate_retry')return renderCandidateRetry(" in html


def test_candidate_retry_view_has_no_master_selection_path() -> None:
    """部分候选态视图不得复用主图选择视图，且不提供任何选图/确认主图元素。"""
    body = _extract_js_function(FRONTEND_HTML.read_text(encoding="utf-8"), "renderCandidateRetry")
    assert "renderCandidates(" not in body
    for token in ("data-candidate", "select-button", "selected_id", "aria-pressed", '<button class="candidate"'):
        assert token not in body, f"部分候选态视图仍包含选图通路标记: {token}"
    assert 'data-action="retry"' in body and "仅补跑失败槽位" in body


def test_selected_id_submission_is_single_site_and_phase_guarded() -> None:
    """全页面 selected_id 提交点唯一，且被 waiting_master_selection 相位门禁保护。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    sites = [line for line in html.splitlines() if "selected_id" in line]
    assert len(sites) == 1, f"selected_id 提交点不唯一: {sites}"
    assert "waiting_master_selection" in sites[0] and "#select-button" in sites[0]


def test_candidate_retry_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本：3/5 部分态无 selected_id 提交通路
    （含伪造 #select-button 点击的交互负向），补跑动作仍路由到 retry 端点，
    且 waiting_master_selection 正常选图通路不被破坏（正向对照）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 12, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"

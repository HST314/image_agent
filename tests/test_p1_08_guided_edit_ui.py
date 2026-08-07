"""P1-08 前端圈画微调 UI 契约测试。

静态契约断言 + Node DOM shim 交互驱动（横/竖/超宽图坐标落点、DPR 与 letterbox、
越界防护、空 Prompt/空标注、撤销/清空、预览与提交一致性、稳定幂等键、
请求中/失败/成功状态、新图强制复检且旧确认不可用、草稿刷新恢复与降级路径）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_HTML = REPO_ROOT / "frontend" / "index.html"
UI_HARNESS = REPO_ROOT / "frontend_tests" / "guided_edit_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "guided_edit_ui_driver.js"


def test_human_rework_phase_routes_to_guided_edit_editor() -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert "s.phase==='waiting_human_rework')return renderGuidedEdit(" in html


def test_guided_edit_frozen_contract_markers_present() -> None:
    """页面脚本必须提交已冻结契约：source_image_pixels 坐标空间与 guided_edit 字段。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert "source_image_pixels" in html
    assert "guided_edit" in html
    assert "coordinate_space" in html


def test_guided_edit_editor_scope_guardrails() -> None:
    """范围外能力不得出现：套索、蒙版、多图层、对象移动、无限画布。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("lasso", "套索", "蒙版", "mask", "layer", "图层"):
        assert token not in html, f"圈画编辑器出现范围外能力标记: {token}"


def test_guided_edit_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动圈画编辑器全量验收场景。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 40, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"


UI_PROBE = REPO_ROOT / "frontend_tests" / "guided_edit_contract_probe.js"


def test_frontend_payload_validates_against_frozen_backend_contract() -> None:
    """前端真实构建的提交体必须通过冻结的后端契约校验（跨栈一致性）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_PROBE)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert results[0]["pass"] and results[0]["body"]
    payload = results[0]["body"]

    import main_front
    request = main_front.AdvanceRequest.model_validate({**payload, "offline": False})
    guided = request.guided_edit
    assert guided is not None
    assert guided.coordinate_space == "source_image_pixels"
    assert guided.parent_asset_id == "artifact_" + "a" * 64
    assert guided.branch == "main"
    assert (guided.source_width, guided.source_height) == (1600, 900)
    assert guided.round == 2  # 探针历史中已有一次当前分支的圈画微调
    assert guided.actor == "op-1"
    assert guided.prompt == "把圈出的区域改为蓝色"
    rect, brush = guided.annotations
    assert (rect.x, rect.y, rect.width, rect.height) == (200, 100, 200, 200)
    assert brush.points[0].x == 600 and brush.points[-1].y == 520

    mutated = json.loads(json.dumps(payload))
    mutated["guided_edit"]["coordinate_space"] = "css_pixels"
    with pytest.raises(ValidationError):
        main_front.AdvanceRequest.model_validate({**mutated, "offline": False})

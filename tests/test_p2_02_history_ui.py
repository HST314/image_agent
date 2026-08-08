"""P2-02 历史时间线前端 UI 契约测试。

静态契约断言 + Node DOM shim 交互驱动（概要分页稳定顺序、选中按需详情、
固化事实不回填当前值、missing/migration_failed/无事实/错误态、历史任务书与图片只读、
资产项目作用域代理、重开只读预览/取消无副作用/确认建分支/版本冲突刷新重试），
以及前端真实建分支载荷对冻结后端 BranchRequest 契约的跨栈校验。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_HTML = REPO_ROOT / "frontend" / "index.html"
UI_HARNESS = REPO_ROOT / "frontend_tests" / "history_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "history_ui_driver.js"
UI_PROBE = REPO_ROOT / "frontend_tests" / "history_contract_probe.js"


def _script() -> str:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    return re.search(r"<script>([\s\S]*?)</script>", html).group(1)


def test_history_section_is_mounted_on_project_view() -> None:
    """时间线挂载在工程视图，打开工程即按服务端稳定顺序加载概要。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert 'id="hist-root"' in html
    assert "renderHistorySection(" in html
    assert "histMaybeLoad(" in html
    assert "histReset()" in html


def test_history_frozen_contract_markers_present() -> None:
    """页面脚本必须消费已冻结契约：概要分页、按需详情、只读预览与建分支字段。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("/history", "?limit=", "cursor=", "next_cursor", "reopen-preview",
                  "expected_version", "/branches", "migration_failed", "missing",
                  "/api/projects/", "availability"):
        assert token in html, f"缺少冻结契约标记: {token}"


def test_history_ui_has_no_writeback_or_edit_entry() -> None:
    """历史时间线不得提供覆盖/写回操作，历史任务书与图片无编辑入口。

    P2-05 起运行时设置台按冻结契约合法使用 PATCH（仅 /api/runtime-settings），
    且只允许出现在 P2-05 模块区域；历史时间线区域与其余模块仍零 PATCH，
    PUT/DELETE 全页面禁止。
    """
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert "contenteditable" not in html
    for token in ("method:'PUT'", "method:'DELETE'",
                  'method:"PUT"', 'method:"DELETE"'):
        assert token not in html, f"页面出现写回能力标记: {token}"
    script = _script()
    stgs_region = re.search(
        r"// ---- P2-05 [\s\S]*?\n    // ---- P2-04 ", script
    )
    assert stgs_region, "缺少 P2-05 运行时设置台模块区域"
    assert "method:'PATCH'" in stgs_region.group(0), "P2-05 区域应包含冻结的 PATCH 修改通道"
    residual = script.replace(stgs_region.group(0), "")
    for token in ("method:'PATCH'", 'method:"PATCH"'):
        assert token not in residual, f"PATCH 只允许出现在 P2-05 设置台区域: {token}"
    for fn in ("renderHistoryDetail", "histFactCard", "histFactBody", "histAssetCard"):
        body = re.search(rf"function {fn}\([\s\S]*?\n    }}", script)
        assert body, f"未找到函数 {fn}"
        assert "snapshot" not in body.group(0), f"{fn} 不得读取当前 snapshot 补画历史"
        assert ".manifest" not in body.group(0), f"{fn} 不得读取当前 manifest 补画历史"


def test_history_ui_scope_guardrails() -> None:
    """范围外能力不得出现：无后台轮询、无前端哈希门禁；事件流仅限 P2-03 监督台。

    P2-03 起 EventSource 由运行监督台按冻结契约合法使用，且只允许出现在
    obsOpenStream 中；历史时间线区域与其余模块仍不得使用事件流。
    """
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("setInterval", "Webhook", "webhook", "crypto.subtle"):
        assert token not in html, f"时间线区域出现范围外能力标记: {token}"
    script = _script()
    obs_open = re.search(r"function obsOpenStream\(\)\s*\{[\s\S]*?\n    \}", script)
    assert obs_open, "缺少 P2-03 监督台的 obsOpenStream"
    residual = script.replace(obs_open.group(0), "")
    assert "EventSource" not in residual, "EventSource 只允许出现在 P2-03 监督台 obsOpenStream"


def test_history_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动时间线全量验收场景。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 70, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"


def test_frontend_reopen_payload_validates_against_frozen_backend_contract() -> None:
    """前端真实构建的建分支提交体必须通过冻结的后端契约校验（跨栈一致性）。"""
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
    request = main_front.BranchRequest.model_validate(payload)
    assert request.checkpoint == "checkpoints/main/000001-confirmation_build.json"
    assert request.name == "revision"
    assert request.actor == "op-9"
    assert request.expected_version == 7

    preview = main_front.HistoryReopenPreviewRequest.model_validate(results[0]["preview_body"])
    assert preview.name is None

    for mutation in (
        {**payload, "expected_version": 0},
        {**payload, "expected_version": None},
        {**payload, "name": "x"},
        {**payload, "actor": ""},
        {**payload, "unexpected_field": "x"},
    ):
        with pytest.raises(ValidationError):
            main_front.BranchRequest.model_validate(mutation)

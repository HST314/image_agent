"""P1-07 轮数上限分流前端 UI 契约测试。

回归目标：click 委托闭包内局部常量 `confirm` 遮蔽全局 `window.confirm`，
导致「继续生成（需确认新费用）」按钮在真实浏览器中抛出
`TypeError: confirm is not a function`、不产生任何请求（发布门禁缺陷）。

覆盖：三个分流按钮真实点击的请求体契约（quality_action / actor /
expense_confirmed / idempotency_key）、continue 费用确认的确认与取消路径、
操作者录入取消路径、重复点击保护，以及全程零异常与零控制台错误。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_HTML = REPO_ROOT / "frontend" / "index.html"
UI_HARNESS = REPO_ROOT / "frontend_tests" / "quality_disposition_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "quality_disposition_ui_driver.js"


def test_disposition_phase_routes_to_quality_panel() -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert "s.phase==='waiting_quality_disposition')return renderQualityDisposition(" in html


def test_delegated_click_handler_does_not_shadow_global_confirm() -> None:
    """click 委托闭包不得再用局部常量遮蔽全局 confirm（费用确认必须命中原生对话框）。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert ",confirm=e.target.closest(" not in html


def test_quality_disposition_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动三个分流按钮的点击验收场景。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 20, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"

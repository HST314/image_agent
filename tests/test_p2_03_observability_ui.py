"""P2-03 运行监督台前端 UI 契约测试。

静态契约断言 + Node DOM shim 交互驱动（稳定分页与冻结高水位、since 增量、
重复/乱序/SSE 断线重连去重、项目切换隔离、五槽/质检/取消/恢复/人工等待真实投影、
409/422/OBSERVABILITY_UNAVAILABLE 错误态、敏感样本不进入 DOM、全流程零业务写入），
以及禁止虚假进度与禁止写接口的护栏。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_HTML = REPO_ROOT / "frontend" / "index.html"
UI_HARNESS = REPO_ROOT / "frontend_tests" / "observability_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "observability_ui_driver.js"


def _script() -> str:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    return re.search(r"<script>([\s\S]*?)</script>", html).group(1)


def _obs_region(script: str) -> str:
    """obs* 监督台模块是连续代码区，整体提取用于作用域护栏。

    P2-06 起健康与诊断台模块位于监督台之后，区域在 P2-06 标记处截止。
    """
    match = re.search(r"// ---- P2-03[\s\S]*?\n    // ---- P2-06 ", script)
    assert match, "缺少 P2-03 监督台模块"
    return match.group(0)


def test_observability_section_is_mounted_on_project_view() -> None:
    """监督台挂载在工程视图，打开工程即加载首页、进度并建立 SSE。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert 'id="obs-root"' in html
    assert "renderObservabilitySection(" in html
    assert "obsMaybeLoad(" in html
    assert "obsReset()" in html


def test_observability_frozen_contract_markers_present() -> None:
    """页面脚本必须消费已冻结契约：event-log 分页/since、progress 投影、SSE 续传与错误语义。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("/event-log", "?limit=", "cursor=", "since=", "through_sequence", "next_cursor",
                  "/progress", "waiting_for_human", "completed_slots", "failed_slots",
                  "current_round", "max_rounds", "at_limit", "retry_count", "attempt",
                  "trace_id", "EventSource", "/events?after=", "lastSeq"):
        assert token in html, f"缺少冻结契约标记: {token}"


def test_observability_ui_has_no_write_or_fabricated_progress() -> None:
    """监督台只允许 GET 读取；禁止百分比/预计时长/计时器等客户端推算进度。"""
    script = _script()
    joined = _obs_region(script)
    for token in ("method:'POST'", "method:'PUT'", "method:'PATCH'", "method:'DELETE'",
                  'method:"POST"', 'method:"PUT"', 'method:"PATCH"', 'method:"DELETE"'):
        assert token not in joined, f"监督台出现写接口标记: {token}"
    for token in ("预计", "预估", "99%", "ETA", "estimated", "countdown"):
        assert token not in joined, f"监督台出现虚假进度标记: {token}"
    assert "setInterval" not in joined, "监督台不得使用周期计时器"
    assert "%" not in joined, "监督台不得出现百分比表达"


def test_observability_ui_never_renders_raw_payload_fields() -> None:
    """事件渲染只允许白名单字段；原始 payload/供应商响应/路径字段不得被引用。"""
    script = _script()
    normalize = re.search(r"function obsNormalize\([\s\S]*?\n    \}", script)
    row = re.search(r"function obsEventRow\([\s\S]*?\n    \}", script)
    assert normalize and row, "缺少 obsNormalize/obsEventRow"
    allowed = ("event_id", "sequence", "event_type", "phase", "job_id", "slot", "round",
               "status", "timestamp", "trace_id", "retry_count", "error_code", "message")
    for forbidden in ("payload", "provider", "raw_response", "signed_url", "local_path",
                      "stack", "exception", "Authorization"):
        assert forbidden not in row.group(0), f"事件行渲染引用了非白名单字段: {forbidden}"
    for field in allowed:
        assert field in normalize.group(0), f"obsNormalize 缺少白名单字段: {field}"


def test_observability_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动监督台全量验收场景。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 55, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"


def test_observability_requests_match_frozen_backend_routes() -> None:
    """前端构造的 event-log 请求与冻结后端路由参数语义一致（limit 上限内、cursor 不透明透传、since 增量）。"""
    script = _script()
    assert re.search(r"OBS_PAGE=50\b", script), "监督台页大小必须为冻结上限内的 50"
    assert re.search(r"function obsPullNew", script) and "since=" in script
    assert "cursor=${encodeURIComponent(cursor)}" in script, "cursor 必须不透明透传"
    assert re.search(r"/events\?after=\$\{obsUi\.lastSeq\}", script), "SSE 必须从最后已确认 sequence 续传"

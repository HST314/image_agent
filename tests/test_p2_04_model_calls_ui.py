"""P2-04 模型调用追溯台前端 UI 契约测试。

静态契约断言 + Node DOM shim 交互驱动（概要列表分页/按需加载、候选与质检反查父子链、
文本增量断点续传与 final_response 终态覆盖、图片真实状态事件零 token、宿主 RBAC 详情门禁、
403/404/409/422/503 可恢复错误态、伪造/跨项目 ID 拒绝、项目切换与迟到响应隔离、
敏感字段不落 DOM、全流程零业务写入），以及禁止虚假进度与禁止写接口的护栏。
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
UI_HARNESS = REPO_ROOT / "frontend_tests" / "model_calls_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "model_calls_ui_driver.js"


def _script() -> str:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    return re.search(r"<script>([\s\S]*?)</script>", html).group(1)


def _trace_region(script: str) -> str:
    """trace* 追溯台模块是连续代码区（P2-04 标记到 P2-03 标记之间），整体提取用于作用域护栏。"""
    match = re.search(r"// ---- P2-04[\s\S]*?\n    // ---- P2-03", script)
    assert match, "缺少 P2-04 追溯台模块"
    return match.group(0)


def test_trace_section_is_mounted_on_project_view() -> None:
    """追溯台挂载在工程视图，打开工程即加载首页概要；候选与质检提供反查入口。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert 'id="trace-root"' in html
    assert "renderTraceSection(" in html
    assert "traceMaybeLoad(" in html
    assert "traceReset()" in html
    assert "bindTrace(" in html
    for fn in ("renderCandidates", "renderCalibration"):
        match = re.search(rf"function {fn}\([\s\S]*?\n    \}}", _script())
        assert match and "data-trace-call" in match.group(0), f"{fn} 缺少调用链反查入口"
    assert "traceFocusCall" in html, "缺少反查定位交互"


def test_trace_frozen_contract_markers_present() -> None:
    """页面脚本必须消费已冻结契约：列表游标分页、概要 DTO、文本增量续传、图片状态事件与错误语义。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("/model-calls", "?limit=", "cursor=", "next_cursor", "detail=true",
                  "/text-deltas", "after=", "next_after", "complete", "final_response",
                  "model_call_id", "parent_call_id", "config_hash", "input_asset_hashes",
                  "prompt_summary", "result_summary", "status_events", "duration_ms",
                  "trace_id", "call_type", "sequence",
                  "已入队", "生成中", "供应商已返回", "资产已入库",
                  "repair 修复调用", "调用列表游标已失效", "查询参数未通过校验",
                  "调用记录不存在或不可见", "调用审计暂不可用", "MODEL_CALL_DETAIL_FORBIDDEN"):
        assert token in html, f"缺少冻结契约标记: {token}"


def test_trace_ui_has_no_write_or_fabricated_progress() -> None:
    """追溯台只允许 GET 读取；禁止百分比/预计时长/计时器等客户端推算进度与虚假状态。"""
    script = _script()
    joined = _trace_region(script)
    for token in ("method:'POST'", "method:'PUT'", "method:'PATCH'", "method:'DELETE'",
                  'method:"POST"', 'method:"PUT"', 'method:"PATCH"', 'method:"DELETE"'):
        assert token not in joined, f"追溯台出现写接口标记: {token}"
    for token in ("预计", "预估", "99%", "estimated", "countdown"):
        assert token not in joined, f"追溯台出现虚假进度标记: {token}"
    assert "setInterval" not in joined, "追溯台不得使用周期计时器"
    assert "EventSource" not in joined, "追溯台不得另开事件流（SSE 只属于 P2-03 监督台）"
    assert "%" not in joined, "追溯台不得出现百分比表达"


def test_trace_detail_gate_only_requests_under_host_rbac() -> None:
    """detail=true 只允许出现在 traceLoadDetail 内，且必须先过宿主 RBAC 门禁；UI 不提供删除审计事实的动作。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    script = _script()
    assert "globalThis.MODEL_CALL_DETAIL_RBAC===true" in script, "缺少宿主 RBAC 声明检测"
    assert html.count("detail=true") == 2, "detail=true 只允许出现在模块注释与 traceLoadDetail 请求中"
    body = re.search(r"function traceLoadDetail\([\s\S]*?\n    \}", script)
    assert body, "缺少 traceLoadDetail"
    text = body.group(0)
    assert "detail=true" in text, "detail=true 请求必须收敛在 traceLoadDetail 内"
    assert text.index("traceHostDetailEnabled()") < text.index("detail=true"), "详情请求前必须先过宿主 RBAC 门禁"
    assert "if(!traceHostDetailEnabled())return''" in script, "宿主未声明 RBAC 时详情入口不得渲染"
    joined = _trace_region(script)
    assert "btn--danger" not in joined and "data-delete" not in joined, "追溯台不得提供删除/破坏审计事实的动作"


def test_trace_summary_whitelist_and_sensitive_fields() -> None:
    """概要渲染只消费白名单 DTO 字段；详情各字段必须经 traceSanitize/traceRedactText 脱敏后才允许进入 DOM。"""
    script = _script()
    normalize = re.search(r"function traceNormalizeSummary\([\s\S]*?\n    \}", script)
    assert normalize, "缺少 traceNormalizeSummary"
    for field in ("call_id", "capability", "call_type", "status", "started_at", "completed_at",
                  "duration_ms", "config_hash", "parent_call_id", "trace_id", "model", "template",
                  "input_asset_hashes", "prompt_summary", "result_summary", "error", "status_events"):
        assert field in normalize.group(0), f"traceNormalizeSummary 缺少白名单字段: {field}"
    for fn in ("renderTraceList", "renderTracePanel", "renderTraceChain", "renderTraceImage", "renderTraceText"):
        match = re.search(rf"function {fn}\([\s\S]*?\n    \}}", script)
        assert match, f"缺少 {fn}"
        for forbidden in ("payload", "raw_response", "signed_url", "local_path", "messages",
                          "output_raw", "output_parsed", "stack", "exception", "Authorization",
                          "api_key", "secret"):
            assert forbidden not in match.group(0), f"{fn} 引用了非白名单字段: {forbidden}"
    detail = re.search(r"function traceNormalizeDetail\([\s\S]*?\n    \}", script)
    assert detail and "traceSanitize" in detail.group(0) and "traceRedactText" in detail.group(0), \
        "详情字段必须经展示层脱敏后才允许渲染"
    assert "TRACE_SECRET_KEY" in script, "缺少敏感键名脱敏规则"


def test_trace_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动追溯台全量验收场景。"""
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


def test_trace_requests_match_frozen_backend_routes() -> None:
    """前端构造的请求与冻结后端路由参数语义一致（limit 上限内、cursor 不透明透传、after 稳定序号续传、ID 格式前置校验）。"""
    script = _script()
    assert re.search(r"TRACE_PAGE=25\b", script), "调用列表页大小必须为冻结上限内的 25"
    assert re.search(r"TRACE_TEXT_PAGE=100\b", script), "文本增量页大小必须为冻结上限 100"
    assert re.search(r"TRACE_CHAIN_MAX=8\b", script), "父子链反查必须有上限截断"
    assert "cursor=${encodeURIComponent(cursor)}" in script, "cursor 必须不透明透传"
    assert "after=${traceUi.textAfter}&limit=${TRACE_TEXT_PAGE}" in script, "文本增量必须从最后已确认序号续传"
    assert re.search(r"TRACE_CALL_ID=/\^call_\[a-f0-9\]\{32\}\$/", script), "伪造/畸形调用 ID 必须在请求前被拦截"
    assert re.search(r"TRACE_HASH=/\^\[a-f0-9\]\{64\}\$/", script), "哈希字段必须按 64 位十六进制校验"

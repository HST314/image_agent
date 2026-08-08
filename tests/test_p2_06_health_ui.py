"""P2-06 健康与诊断台前端 UI 契约测试。

静态契约断言 + Node DOM shim 交互驱动（liveness/readiness 分层、六组件及组合状态映射、
离线/供应商未配置提示、关键 503 与局部退化 200、trace 关联、reader/operator 无诊断入口、
admin 受控读取、敏感信息负向用例、限频轮询去重、错误恢复、页面/项目/设置切换停止与迟到响应隔离、
全流程零业务写入），以及禁止写接口/周期计时器/事件流的作用域护栏。
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
UI_HARNESS = REPO_ROOT / "frontend_tests" / "health_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "health_ui_driver.js"


def _html() -> str:
    return FRONTEND_HTML.read_text(encoding="utf-8")


def _script() -> str:
    return re.search(r"<script>([\s\S]*?)</script>", _html()).group(1)


def _hlth_region() -> str:
    """hlth* 健康与诊断台模块是连续代码区（P2-06 标记到 renderTimeline 之间）。"""
    region = re.search(r"// ---- P2-06 [\s\S]*?\n    function renderTimeline", _script())
    assert region, "缺少 P2-06 健康与诊断台模块区域"
    return region.group(0)


def test_health_view_is_mounted_as_global_section() -> None:
    """诊断台为全局视图：顶栏入口、随导航重置、定向重渲染挂载点。"""
    html = _html()
    assert 'id="health-button"' in html
    script = _script()
    assert "hlthOpen(" in script
    assert "hlthReset()" in script
    assert 'id="hlth-root"' in script
    assert "renderHealthSection(" in script


def test_health_frozen_contract_markers_present() -> None:
    """页面脚本必须消费已冻结契约：live/ready/diagnostics 端点、六组件与稳定语义字段。"""
    region = _hlth_region()
    for token in ("/api/health/live", "/api/health/ready", "/api/internal/diagnostics/",
                  "model_router", "worker", "queue", "storage", "event_append", "asset_proxy",
                  "checked_at", "business_impact", "error_code", "trace_id",
                  "not_ready", "degraded", "not_configured", "offline",
                  "OFFLINE_ONLY", "MODEL_PROVIDER_NOT_CONFIGURED", "RUNTIME_SETTINGS_ROLE"):
        assert token in region, f"P2-06 区域缺少冻结契约标记: {token}"


def test_health_ui_has_no_write_or_out_of_scope_capability() -> None:
    """健康台只允许 GET 读取；禁止周期计时器、事件流与调试输出。"""
    region = _hlth_region()
    for token in ("method:'POST'", "method:'PUT'", "method:'PATCH'", "method:'DELETE'",
                  'method:"POST"', 'method:"PUT"', 'method:"PATCH"', 'method:"DELETE"'):
        assert token not in region, f"健康台出现写接口标记: {token}"
    assert "setInterval" not in region, "健康台不得使用周期计时器（限频轮询由 setTimeout 链实现）"
    assert "EventSource" not in region, "健康台不得另开事件流（SSE 只属于 P2-03 监督台）"
    assert "console.log" not in region, "健康台禁止调试输出"
    fetches = list(re.finditer(r"await hlthFetchRaw\(`?([^`,)]*)`?\)", region))
    assert len(fetches) == 3, f"健康台读取通道数量异常: {len(fetches)}"
    targets = [f.group(1) for f in fetches]
    assert any("/api/health/live" in t for t in targets)
    assert any("/api/health/ready" in t for t in targets)
    assert any("/api/internal/diagnostics/" in t for t in targets)


def test_health_public_render_whitelist_guards() -> None:
    """公共渲染只读 status/error_code/checked_at/business_impact；内部探针字段不得被公共视图引用。"""
    region = _hlth_region()
    card = re.search(r"function hlthComponentCard\([\s\S]*?\n    \}", region)
    live = re.search(r"function renderHealthLive\([\s\S]*?\n    \}", region)
    ready = re.search(r"function renderHealthReady\([\s\S]*?\n    \}", region)
    assert card and live and ready, "缺少健康台公共渲染函数"
    for fn in (card.group(0), live.group(0), ready.group(0)):
        for forbidden in ("anchor", "free_bytes", "stale_jobs", "stale_count", "exception",
                          "stack", "projects_root", "payload", "provider_raw"):
            assert forbidden not in fn, f"公共渲染引用了内部探针字段: {forbidden}"
        assert "escapeHtml" in fn, "公共渲染必须经过 HTML 转义"


def test_health_diagnostics_acl_double_gate_static() -> None:
    """内部诊断：前端入口仅 admin（服务端 ACL 仍是最终裁决）；非法 trace 在请求前拦截。"""
    region = _hlth_region()
    open_fn = re.search(r"function hlthOpenDiagnostics\([\s\S]*?\n    \}", region)
    assert open_fn, "缺少 hlthOpenDiagnostics"
    body = open_fn.group(0)
    assert "hlthRole()!=='admin'" in body, "诊断入口必须仅 admin"
    assert "HLTH_TRACE_RE.test" in body, "非法 trace 必须在发出请求前拦截"
    assert body.index("hlthRole()!=='admin'") < body.index("hlthFetchRaw"), "角色门禁必须先于请求"
    assert body.index("HLTH_TRACE_RE.test") < body.index("hlthFetchRaw"), "trace 校验必须先于请求"
    ready = re.search(r"function renderHealthReady\([\s\S]*?\n    \}", region)
    assert ready and "hlthRole()==='admin'" in ready.group(0), "诊断按钮必须只在 admin 角色渲染"


def test_health_polling_is_bounded_and_stoppable_static() -> None:
    """轮询限频：in-flight 去重、setTimeout 链、离开视图即停。"""
    region = _hlth_region()
    load_fn = re.search(r"async function hlthLoad\([\s\S]*?\n    \}", region)
    assert load_fn and "hlthUi.inFlight" in load_fn.group(0), "健康台必须有 in-flight 去重"
    schedule = re.search(r"function hlthScheduleNext\([\s\S]*?\n    \}", region)
    assert schedule and "setTimeout" in schedule.group(0) and "hlthUi.active" in schedule.group(0)
    reset = re.search(r"function hlthReset\([\s\S]*?\n    \}", region)
    assert reset and "clearTimeout" in reset.group(0), "重置必须停止轮询计时器"
    script = _script()
    for hook in ("function renderHome(", "async function openProject(", "function stgsOpen("):
        fn = re.search(re.escape(hook) + r"[\s\S]*?\n    \}", script)
        assert fn and "hlthReset()" in fn.group(0), f"{hook} 必须重置健康台（页面切换即停）"


def test_health_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动健康台全量验收场景。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 100, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"

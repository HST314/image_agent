"""P1-10 前端统一错误呈现 UI 契约测试。

静态契约断言（映射表完整性、范围护栏）+ Node DOM shim 交互驱动
（全部稳定 code 与五类建议动作、429/Retry-After、可重试与不可重试按钮差异、
部分槽位恢复呈现、刷新/worker 重启状态一致、超过上限不再给重试、
取消与人工等待差异、缺失可选字段与旧错误对象兼容、敏感详情转义、
重复点击单请求、HTTP 422 与异步 job failed 边界），
以及前端消费 fixture 对���结 AsyncJob v1 schema 的跨栈校验。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_HTML = REPO_ROOT / "frontend" / "index.html"
UI_HARNESS = REPO_ROOT / "frontend_tests" / "error_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "error_ui_driver.js"
UI_PROBE = REPO_ROOT / "frontend_tests" / "error_contract_probe.js"
JOB_SCHEMA = json.loads((REPO_ROOT / "schemas" / "AsyncJob.v1.schema.json").read_text(encoding="utf-8"))
ERROR_SCHEMA = next(branch for branch in JOB_SCHEMA["properties"]["error"]["oneOf"] if branch.get("type") == "object")

STABLE_CODES = [
    "UPSTREAM_TIMEOUT", "RATE_LIMITED", "AUTHENTICATION_FAILED", "CONTENT_REJECTED",
    "PROVIDER_UNAVAILABLE", "ASSET_INGESTION_FAILED", "STRUCTURED_OUTPUT_INVALID",
    "INVALID_INPUT", "CONFIGURATION_OR_SKILL", "CANCELLED", "INTERNAL_ERROR",
]
SUGGESTED_ACTIONS = ["retry", "modify_input", "contact_admin", "human_decision", "none"]


def test_error_mapping_table_covers_frozen_contract() -> None:
    """前端映射表必须覆盖全部稳定 code 与五类建议动作。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for code in STABLE_CODES:
        assert code in html, f"缺少稳定错误码映射: {code}"
    for action in SUGGESTED_ACTIONS:
        assert action in html, f"缺少建议动作映射: {action}"
    assert set(STABLE_CODES) == set(ERROR_SCHEMA["properties"]["code"]["enum"])
    assert set(SUGGESTED_ACTIONS) == set(ERROR_SCHEMA["properties"]["suggested_action"]["enum"])


def test_error_ui_consumes_frozen_channels() -> None:
    """页面必须统一消费 AsyncJob 与 checkpoint 通道：轮询、取消、Retry-After、幂等复用。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("status_url", "/cancel", "retry_after_seconds", "job-watch:",
                  "jobRetryOriginal", "errInfo", "renderErrPanel"):
        assert token in html, f"缺少冻结通道标记: {token}"


def test_error_ui_scope_guardrails() -> None:
    """范围外能力不得出现：无后台定时轮询、无 Webhook、无前端密钥计算；事件流仅限 P2-03 监督台。

    P2-03 起 EventSource 由运行监督台按冻结契约合法使用，且只允许出现在
    obsOpenStream 中；错误呈现与其余模块仍不得使用事件流。
    """
    import re

    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("setInterval", "Webhook", "webhook", "crypto.subtle"):
        assert token not in html, f"错误呈现出现范围外能力标记: {token}"
    script = re.search(r"<script>([\s\S]*?)</script>", html).group(1)
    obs_open = re.search(r"function obsOpenStream\(\)\s*\{[\s\S]*?\n    \}", script)
    assert obs_open, "缺少 P2-03 监督台的 obsOpenStream"
    assert "EventSource" not in script.replace(obs_open.group(0), ""), "EventSource 只允许出现在 P2-03 监督台 obsOpenStream"


def test_waiting_phases_have_no_failure_semantics() -> None:
    """waiting_* 相位渲染分支不得携带失败面板或错误重试标记（失败面板独立分支先行）。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    stage = html.split("function renderStage", 1)[1]
    assert "if(m.failed_step||wj)return renderFailureStage(s,m);" in stage
    waiting_branches = stage.split("if(m.failed_step||wj)return renderFailureStage(s,m);", 1)[1]
    assert "renderErrPanel(" not in waiting_branches, "waiting 分支不得渲染错误面板"


def test_error_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动错误呈现全量验收场景。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 90, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"


def test_frontend_consumed_fixtures_validate_against_frozen_schema() -> None:
    """前端真实消费的稳定错误对象 fixture 必须通过冻结 AsyncJob v1 schema，
    且前端映射结果覆盖 schema 全部 code 与 suggested_action 枚举。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_PROBE)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert results[0]["pass"] and results[0]["fixtures"]

    validator = Draft202012Validator(ERROR_SCHEMA)
    for fixture in results[0]["fixtures"]:
        assert list(validator.iter_errors(fixture)) == [], f"fixture 不满足冻结 schema: {fixture}"

    mapped = results[0]["mapped"]
    assert {item["code"] for item in mapped} == set(STABLE_CODES)
    assert {item["action"] for item in mapped} <= set(SUGGESTED_ACTIONS)
    for item in mapped:
        assert item["stable"] is True
    by_code = {item["code"]: item for item in mapped}
    assert by_code["RATE_LIMITED"]["retryAfter"] == 4
    assert by_code["UPSTREAM_TIMEOUT"]["slot"] == 2
    assert by_code["CONTENT_REJECTED"]["round"] == 2

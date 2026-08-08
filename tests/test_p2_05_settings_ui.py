"""P2-05 运行时设置台前端 UI 契约测试。

静态契约断言 + Node DOM shim 交互驱动（schema 驱动渲染、reader/operator/admin 入口矩阵、
客户端校验与暂存、危险项二次确认、409/403/422/503 可恢复、密钥端到端零回显、按需详情、
项目级“从 checkpoint 新建分支并应用”、刷新与迟到响应隔离、零意外写入），
以及前端真实提交的保存/密钥/应用载荷对冻结后端
RuntimeSettingsUpdateRequest / BranchSettingsApplyRequest 契约的跨栈校验。
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
UI_HARNESS = REPO_ROOT / "frontend_tests" / "settings_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "settings_ui_driver.js"
UI_PROBE = REPO_ROOT / "frontend_tests" / "settings_contract_probe.js"


def _html() -> str:
    return FRONTEND_HTML.read_text(encoding="utf-8")


def _script() -> str:
    return re.search(r"<script>([\s\S]*?)</script>", _html()).group(1)


def _stgs_region() -> str:
    region = re.search(r"// ---- P2-05 [\s\S]*?\n    // ---- P2-04 ", _script())
    assert region, "缺少 P2-05 运行时设置台模块区域"
    return region.group(0)


def test_settings_view_is_mounted_as_global_section() -> None:
    """设置台为全局视图：顶栏入口、静态确认/应用对话框、随导航重置。"""
    html = _html()
    assert 'id="settings-button"' in html
    assert 'id="stgs-danger-dialog"' in html
    assert 'id="stgs-apply-dialog"' in html
    script = _script()
    assert "stgsOpen(" in script
    assert "stgsReset()" in script
    assert 'id="stgs-root"' in script


def test_settings_frozen_contract_markers_present() -> None:
    """页面脚本必须消费已冻结契约：describe/PATCH/单键详情/分支应用与宿主角色注入。"""
    region = _stgs_region()
    for token in ("/api/runtime-settings", "expected_version", "dangerous_confirmed",
                  "settings_version", "apply-runtime-settings", "RUNTIME_SETTINGS_ROLE",
                  "secret_state", "effective_when", "provider_api_key", "schema_version"):
        assert token in region, f"P2-05 区域缺少冻结契约标记: {token}"


def test_settings_write_methods_scoped_to_frozen_endpoints() -> None:
    """P2-05 区域内只允许 PATCH 设置与 POST 分支应用两个写通道，无 PUT/DELETE。"""
    region = _stgs_region()
    for token in ("method:'PUT'", "method:'DELETE'", 'method:"PUT"', 'method:"DELETE"'):
        assert token not in region, f"P2-05 区域出现范围外写方法: {token}"
    writes = list(re.finditer(r"stgsFetch\((?:[^()]|\([^()]*\))*?method:'(PATCH|POST)'", region))
    assert len(writes) == 3, f"P2-05 区域写通道数量异常: {len(writes)}"
    for fetch in writes:
        call, method = fetch.group(0), fetch.group(1)
        if method == "PATCH":
            assert call.startswith("stgsFetch('/api/runtime-settings'"), f"PATCH 只允许打向冻结设置端点: {call[:80]}"
        else:
            assert "branches/apply-runtime-settings" in call, f"POST 只允许打向分支应用端点: {call[:80]}"


def test_settings_secret_never_echoed_static_guards() -> None:
    """密钥只显 unset/set：密码输入、草稿即时擦除、敏感字段渲染不读 value、无调试泄漏。"""
    region = _stgs_region()
    assert 'type="password"' in region
    assert "autocomplete=\"off\"" in region
    assert "密钥不回显" in region
    display = re.search(r"function stgsDisplayValue\([\s\S]*?\n    \}", region)
    assert display and "secret_state" in display.group(0), "敏感字段当前值必须来自 secret_state"
    assert "f.value" not in display.group(0).split("?")[0], "敏感分支不得读取 f.value"
    assert region.count("stgsSecretDraft=''") >= 3, "草稿必须在重置/关闭/取消/提交后即时擦除"
    assert "console.log" not in region, "设置台禁止调试输出（防密钥进入日志）"
    save = re.search(r"function stgsDoSave\([\s\S]*?\n    \}", region)
    assert save and "provider_api_key" not in save.group(0), "普通保存通道不得携带密钥字段"


def test_settings_rbac_frontend_gates_present() -> None:
    """前端仅按宿主注入角色控制入口与提示，并明示服务端最终鉴权。"""
    region = _stgs_region()
    assert "globalThis.RUNTIME_SETTINGS_ROLE" in region
    assert "function stgsCanEdit(" in region
    assert "服务端最终鉴权" in region
    role = re.search(r"function stgsRole\(\)[\s\S]*?\n    \}", region)
    assert role and "reader" in role.group(0), "缺省/未知角色必须安全回退为只读"
    apply_gate = re.search(r"function stgsApplyOpen\(\)\{[\s\S]*?\n    \}", region)
    assert apply_gate and "stgsRole()!=='admin'" in apply_gate.group(0), "项目级应用入口必须仅管理员可见"


def test_settings_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动设置台全量验收场景。"""
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


def test_frontend_payloads_validate_against_frozen_backend_contract() -> None:
    """前端真实构建的保存/密钥/应用提交体必须通过冻结的后端契约校验（跨栈一致性）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_PROBE)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    result = json.loads(proc.stdout)[0]
    assert result["pass"] and result["body"] and result["secret_body"] and result["apply_body"]

    import main_front

    save = main_front.RuntimeSettingsUpdateRequest.model_validate(result["body"])
    assert save.expected_version == 7
    assert save.actor == "probe-admin"
    assert save.changes == {"question_mode": "manual", "candidate_count": 9}
    assert save.dangerous_confirmed is True  # candidate_count 为高风险，必须显式确认

    secret = main_front.RuntimeSettingsUpdateRequest.model_validate(result["secret_body"])
    assert secret.changes == {"provider_api_key": "sk-probe-secret-789"}
    assert secret.dangerous_confirmed is True

    apply = main_front.BranchSettingsApplyRequest.model_validate(result["apply_body"])
    assert apply.checkpoint == "checkpoints/main/000005-final_confirmation.json"
    assert apply.name == "settings-branch-1"
    assert apply.actor == "probe-admin"
    assert apply.expected_version == 11
    assert apply.settings_version == 9

    for mutation in (
        {**result["body"], "expected_version": 0},
        {**result["body"], "expected_version": None},
        {**result["body"], "changes": {}},
        {**result["body"], "actor": ""},
        {**result["body"], "unexpected_field": "x"},
    ):
        with pytest.raises(ValidationError):
            main_front.RuntimeSettingsUpdateRequest.model_validate(mutation)

    for mutation in (
        {**result["apply_body"], "settings_version": 0},
        {**result["apply_body"], "settings_version": None},
        {**result["apply_body"], "expected_version": 0},
        {**result["apply_body"], "name": "x"},
        {**result["apply_body"], "offline": True},
    ):
        with pytest.raises(ValidationError):
            main_front.BranchSettingsApplyRequest.model_validate(mutation)

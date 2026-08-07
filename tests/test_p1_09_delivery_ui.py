"""P1-09 前端交付与人工回传 UI 契约测试。

静态契约断言 + Node DOM shim 交互驱动（未生成/生成中/失败可重试/待回传/已回传、
三段说明与资产元数据完整呈现、checkpoint 不可读时仅靠 Delivery 渲染、
重复点击单请求与同载荷稳定幂等键、回传成功展示 actor/时间/目标/版本、
409 与网络失败可恢复、版本变化不沿用旧回传 UI 状态、XSS 转义、无后台轮询），
以及前端真实回传载荷对冻结后端 ManualReturnRequest 契约的跨栈校验。
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
UI_HARNESS = REPO_ROOT / "frontend_tests" / "delivery_ui_harness.mjs"
UI_DRIVER = REPO_ROOT / "frontend_tests" / "delivery_ui_driver.js"
UI_PROBE = REPO_ROOT / "frontend_tests" / "delivery_contract_probe.js"


def test_completed_phase_routes_to_delivery_section() -> None:
    """最终确认后的交付区域挂载在 completed 相位，并读取独立 GET /delivery。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    assert "if(s.completed)return" in html
    assert 'id="dlv-root"' in html
    assert "renderDeliverySection(" in html
    assert "loadDelivery(" in html


def test_delivery_frozen_contract_markers_present() -> None:
    """页面脚本必须消费已冻结契约：独立 Delivery 读取、生成与人工回传字段。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("/delivery/generate", "/delivery/return", "delivery_version",
                  "idempotency_key", "delivery-return:", "pending_return"):
        assert token in html, f"缺少冻结契约标记: {token}"


def test_delivery_ui_scope_guardrails() -> None:
    """范围外能力不得出现：无后台轮询、无外部事件流、无通知/Webhook、无前端哈希门禁。"""
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for token in ("EventSource", "setInterval", "Webhook", "webhook", "crypto.subtle"):
        assert token not in html, f"交付区域出现范围外能力标记: {token}"


def test_delivery_ui_dom_and_interaction_contract() -> None:
    """在 Node DOM shim 中真实执行页面脚本，驱动交付区域全量验收场景。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime unavailable for DOM interaction harness")
    proc = subprocess.run(
        [node, str(UI_HARNESS), str(FRONTEND_HTML), str(UI_DRIVER)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    assert len(results) >= 45, f"UI 契约断言数量异常: {len(results)}"
    failed = [item["name"] for item in results if not item["pass"]]
    assert not failed, f"UI 契约失败项: {failed}"


def test_frontend_return_payload_validates_against_frozen_backend_contract() -> None:
    """前端真实构建的回传提交体必须通过冻结的后端契约校验（跨栈一致性）。"""
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
    request = main_front.ManualReturnRequest.model_validate(payload)
    assert request.delivery_version == 1
    assert request.actor == "op-1"
    assert request.target == "parent-agent"
    assert request.idempotency_key.startswith("delivery-return:")

    for mutation in (
        {**payload, "actor": ""},
        {**payload, "target": ""},
        {**payload, "delivery_version": 0},
        {**payload, "idempotency_key": "bad key with spaces"},
        {**payload, "unexpected_field": "x"},
    ):
        with pytest.raises(ValidationError):
            main_front.ManualReturnRequest.model_validate(mutation)

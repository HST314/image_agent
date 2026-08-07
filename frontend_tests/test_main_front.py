"""FastAPI 薄适配层的契约与安全测试。"""
from __future__ import annotations

from pathlib import Path
import base64

import pytest
from fastapi.testclient import TestClient

import main_front


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    return TestClient(main_front.app, raise_server_exceptions=False)


def test_health_and_frontend_are_served(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    assert "Image Agent Studio" in page.text
    assert "prefers-reduced-motion" in page.text
    assert "artifact:\\/\\/" in page.text
    assert "projects_root" not in client.get("/api/health").json()


@pytest.mark.parametrize("project_id", ["../escape", "a", "含中文", "bad/id"])
def test_project_id_rejects_path_traversal(client: TestClient, project_id: str) -> None:
    response = client.get(f"/api/projects/{project_id}")
    assert response.status_code in {404, 422}


def test_oversized_request_is_rejected_before_json_parse(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        content=b"x" * (main_front.MAX_REQUEST_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_unknown_project_returns_real_not_found(client: TestClient) -> None:
    response = client.get("/api/projects/not-created")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_asset_endpoint_rejects_unsupported_type(client: TestClient) -> None:
    store = main_front.ProjectStore(main_front.PROJECTS_ROOT, "safe-project")
    store.create()
    asset_dir = store.root / "artifacts" / "images"
    asset_dir.mkdir(parents=True)
    (asset_dir / "note.txt").write_text("not an image", encoding="utf-8")
    response = client.get("/api/projects/safe-project/assets/note.txt")
    assert response.status_code == 422


def test_asset_endpoint_resolves_project_scoped_artifact_id(client: TestClient) -> None:
    one = main_front.ProjectStore(main_front.PROJECTS_ROOT, "project-one"); one.create()
    two = main_front.ProjectStore(main_front.PROJECTS_ROOT, "project-two"); two.create()
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    saved = one.artifacts.save_bytes(png, suffix=".png", metadata={"media_type": "image/png"})
    ok = client.get(f'/api/projects/project-one/assets/{saved["artifact_id"]}')
    assert ok.status_code == 200 and ok.content == png
    assert client.get(f'/api/projects/project-two/assets/{saved["artifact_id"]}').status_code == 404
    assert client.get("/api/projects/project-one/assets/..%2Fmanifest.json").status_code in {404, 422}


def test_offline_project_stops_at_a_real_waiting_checkpoint(client: TestClient) -> None:
    task = {
        "task_id": "task-web-test",
        "project_id": "web-test",
        "source_refs": [{"ref_id": "brief-1", "ref_type": "brief", "excerpt": "测试创作输入", "source_hash": None}],
        "deliverable_goal": "生成一张用于内部审核的极简产品视觉图",
        "usage_context": "内部审核",
        "category_ref": {"category_id": "generic_visual_delivery", "version": "1.0"},
        "known_facts": {"audience": "审核人员"},
        "unknowns": {"output_spec": "待确认"},
        "asset_inputs": [],
        "status": "draft",
    }
    response = client.post("/api/projects", json={"project_id": "web-test", "task_card": task, "offline": True})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["snapshot"]["state"] == "intake_clarify"
    assert data["manifest"]["current_checkpoint"]["sequence"] == 1
    assert data["snapshot"].get("completed") is not True


def test_http_task_spec_confirmation_contract(client: TestClient) -> None:
    task = {
        "task_id": "task-confirm", "project_id": "confirm-web",
        "source_refs": [{"ref_id": "brief-1", "ref_type": "brief"}],
        "deliverable_goal": "海报", "usage_context": "审核", "known_facts": {"主体": "产品"},
        "unknowns": {}, "asset_inputs": [], "status": "draft",
    }
    created = client.post("/api/projects", json={"project_id":"confirm-web", "task_card":task, "offline":True})
    assert created.status_code == 201
    view = created.json()
    assert view["snapshot"]["phase"] == "waiting_task_spec_confirmation"
    assert "confirm_task_spec" in view["capabilities"]
    confirmed = client.post("/api/projects/confirm-web/advance", json={
        "offline":True, "task_spec_action":"confirm", "actor":"operator-1"
    })
    assert confirmed.status_code == 200
    fact = confirmed.json()["snapshot"]["task_spec_confirmation"]
    assert fact["actor"] == "operator-1" and fact["subject_sha256"]

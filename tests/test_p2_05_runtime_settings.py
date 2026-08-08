import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import validate

import main_front
from configs.runtime_policy import RuntimePolicy
from configs.runtime_settings import FIELD_META, RuntimeSettingsStore, SettingsConflict
from storage.project_store import ProjectStore


def settings(tmp_path: Path) -> RuntimeSettingsStore:
    return RuntimeSettingsStore(tmp_path, RuntimePolicy.from_file(Path("configs/runtime.yaml")))


def test_schema_drives_metadata_defaults_and_reads_do_not_write_business_state(tmp_path: Path):
    store = settings(tmp_path)
    before = set(tmp_path.glob(".runtime-settings*"))
    result = store.describe()
    validate(result, json.loads(Path("schemas/RuntimeSettings.v1.schema.json").read_text()))
    assert {item["key"] for item in result["fields"]} == set(FIELD_META)
    assert next(x for x in result["fields"] if x["key"] == "watermark")["default"] is False
    assert next(x for x in result["fields"] if x["key"] == "provider_api_key")["value"] is None
    assert not store.path.exists() and not store.audit_path.exists()
    assert before == set()


def test_rbac_validation_secret_masking_and_audit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    client = TestClient(main_front.app)
    main_front.app.state.runtime_settings_authorizer = lambda request: request.headers.get("x-role")
    assert client.patch("/api/runtime-settings", json={"expected_version": 1, "actor": "nobody", "changes": {"watermark": True}}).status_code == 403
    denied = client.patch("/api/runtime-settings", headers={"x-role": "operator"}, json={
        "expected_version": 1, "actor": "op", "dangerous_confirmed": True, "changes": {"candidate_count": 4}})
    assert denied.status_code == 403
    changed = client.patch("/api/runtime-settings", headers={"x-role": "admin"}, json={
        "expected_version": 1, "actor": "admin-a", "dangerous_confirmed": True,
        "changes": {"candidate_count": 4, "provider_api_key": "super-secret-value"}})
    assert changed.status_code == 200 and changed.json()["version"] == 2
    for response in (client.get("/api/runtime-settings"), client.get("/api/runtime-settings/provider_api_key"),
                     client.get("/api/runtime-settings/missing")):
        assert "super-secret-value" not in response.text
    assert "super-secret-value" not in settings(tmp_path).audit_path.read_text()
    audit = settings(tmp_path).audit()[0]
    assert audit["actor"] == "admin-a" and audit["before_version"] == 1 and audit["version"] == 2


def test_unknown_invalid_combination_and_danger_confirmation_are_422(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    main_front.app.state.runtime_settings_authorizer = lambda request: "admin"
    client = TestClient(main_front.app)
    common = {"expected_version": 1, "actor": "admin"}
    assert client.patch("/api/runtime-settings", json={**common, "changes": {"not_wired": 1}}).status_code == 422
    assert client.patch("/api/runtime-settings", json={**common, "changes": {"candidate_count": 4}}).status_code == 422
    invalid = client.patch("/api/runtime-settings", json={**common, "dangerous_confirmed": True,
        "changes": {"question_mode": "manual", "max_auto_questions": 2}})
    assert invalid.status_code == 422


def test_optimistic_concurrency_restart_and_secret_state(tmp_path: Path):
    store = settings(tmp_path)
    def update(value: bool):
        try:
            return store.update({"watermark": value}, expected_version=1, actor="op", role="operator", dangerous_confirmed=False)
        except SettingsConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [True, False]))
    assert sum(item == "conflict" for item in results) == 1
    restarted = settings(tmp_path)
    assert restarted.snapshot()["version"] == 2
    assert restarted.audit()[0]["version"] == 2


def test_queued_job_freezes_settings_and_old_project_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    project = ProjectStore(tmp_path, "frozen")
    original = RuntimePolicy.from_file(Path("configs/runtime.yaml")).snapshot("offline")
    project.create({"runtime_policy": original})
    project_file = (project.root / "project.yaml").read_bytes()
    jobs = main_front._job_store("frozen")
    first_settings = settings(tmp_path).snapshot()
    old, _ = jobs.create("old-key-1", {"mode": "offline", "options": {}, "runtime_settings": first_settings})
    settings(tmp_path).update({"watermark": True}, expected_version=1, actor="op", role="operator", dangerous_confirmed=False)
    new_settings = settings(tmp_path).snapshot()
    new, _ = jobs.create("new-key-1", {"mode": "offline", "options": {}, "runtime_settings": new_settings})
    assert jobs.get(old["job_id"])["payload"]["runtime_settings"] == first_settings
    assert jobs.get(new["job_id"])["payload"]["runtime_settings"]["version"] == 2
    assert jobs.get(new["job_id"])["payload"]["runtime_settings"]["policy"]["watermark"] is True
    assert (project.root / "project.yaml").read_bytes() == project_file
    project.assert_runtime_mode("offline")


def test_project_level_application_requires_explicit_new_branch_and_preserves_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    policy = RuntimePolicy.from_file(Path("configs/runtime.yaml"))
    project = ProjectStore(tmp_path, "branch-settings")
    project.create({"runtime_policy": policy.snapshot("offline")})
    checkpoint = project.checkpoint("confirmation_build", {"state": "confirmation_build"})
    old_branch_bytes = (project.root / "branches.json").read_bytes()
    project_bytes = (project.root / "project.yaml").read_bytes()
    settings(tmp_path).update({"image_api_base_url": "https://images.example.test"}, expected_version=1,
                              actor="admin", role="admin", dangerous_confirmed=True)
    assert (project.root / "branches.json").read_bytes() == old_branch_bytes
    main_front.app.state.runtime_settings_authorizer = lambda request: request.headers.get("x-role")
    client = TestClient(main_front.app)
    payload = {"checkpoint": checkpoint, "name": "configured-child", "actor": "admin",
               "expected_version": 2, "settings_version": 2}
    assert client.post("/api/projects/branch-settings/branches/apply-runtime-settings", json=payload).status_code == 403
    response = client.post("/api/projects/branch-settings/branches/apply-runtime-settings",
                           headers={"x-role": "admin"}, json=payload)
    assert response.status_code == 200 and response.json()["runtime_settings_version"] == 2
    child = next(x for x in response.json()["branches"]["items"] if x["name"] == "configured-child")
    assert child["runtime_settings"]["policy"]["image_api_base_url"] == "https://images.example.test"
    assert (project.root / "project.yaml").read_bytes() == project_bytes
    ProjectStore(tmp_path, "branch-settings").assert_runtime_mode("offline")

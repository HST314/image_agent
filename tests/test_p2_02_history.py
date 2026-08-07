import json
from pathlib import Path

from fastapi.testclient import TestClient

import main_front
from storage.project_store import ProjectStore


def _seed(root: Path, project_id: str = "history"):
    store = ProjectStore(root, project_id)
    store.create({"runtime_policy": {"mode": "offline", "schema_version": 1, "policy": {}}})
    first = store.checkpoint("confirmation_build", {
        "phase": "waiting_task_spec_confirmation", "task_specification": {"version": 1, "title": "past"},
        "style_cards": [{"style_index": "STYLE-001", "version": "1"}],
        "candidate_assets": [{"artifact_id": "artifact_" + "1" * 64}],
        "model_output_summary": {"model": "m1", "summary": "old"},
    })
    second = store.checkpoint("initial_candidate_generation", {
        "phase": "waiting_master_selection", "task_specification": {"version": 2, "title": "later"},
        "inspection": {"passed": False}, "human_decision": {"action": "rework"},
    })
    return store, first, second


def test_history_is_paginated_stable_and_details_are_frozen(tmp_path: Path):
    store, first, second = _seed(tmp_path)
    before = {p.relative_to(store.root).as_posix(): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    page1 = store.history_index(limit=1)
    page2 = store.history_index(limit=1, cursor=page1["next_cursor"])
    assert [x["checkpoint"] for x in page1["items"] + page2["items"]] == [first, second]
    assert "detail" not in page1["items"][0]
    old = store.history_detail(page1["items"][0]["node_id"])
    new = store.history_detail(page2["items"][0]["node_id"])
    assert old["facts"]["task_specification"]["title"] == "past"
    assert new["facts"]["task_specification"]["title"] == "later"
    after = {p.relative_to(store.root).as_posix(): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    assert after == before


def test_missing_or_corrupt_history_never_falls_back_to_current(tmp_path: Path):
    store, first, _ = _seed(tmp_path)
    node = store.history_index(limit=10)["items"][0]
    (store.root / first).write_text("{broken", encoding="utf-8")
    detail = store.history_detail(node["node_id"])
    assert detail["availability"] == "migration_failed"
    assert detail["facts"] is None
    (store.root / first).unlink()
    missing = store.history_detail(node["node_id"])
    assert missing["availability"] == "missing" and missing["facts"] is None


def test_preview_and_reopen_reuse_branch_contract_and_preserve_source(tmp_path: Path):
    store, first, _ = _seed(tmp_path)
    node = store.history_index(limit=10)["items"][0]
    preview = store.history_reopen_preview(node["node_id"], name="revision")
    assert preview["parent_branch_id"] and preview["new_branch"]["name"] == "revision"
    assert set(preview["invalidated_confirmations"]) >= {"task_spec_confirmation", "inspection", "final_confirmation"}
    source = (store.root / first).read_bytes()
    store.branch_from(first, name="revision", expected_version=3)
    assert (store.root / first).read_bytes() == source
    assert ProjectStore(tmp_path, "history").history_detail(node["node_id"])["facts"]["task_specification"]["version"] == 1


def test_http_history_rejects_cross_project_node_and_assets(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    one, _, _ = _seed(tmp_path, "one")
    _seed(tmp_path, "two")
    node = one.history_index(limit=10)["items"][0]["node_id"]
    client = TestClient(main_front.app)
    assert client.get("/api/projects/one/history?limit=1").status_code == 200
    assert client.get(f"/api/projects/one/history/{node}").status_code == 200
    assert client.get(f"/api/projects/two/history/{node}").status_code == 404
    assert client.get("/api/projects/two/assets/artifact_" + "1" * 64).status_code == 404
    assert client.patch(f"/api/projects/one/history/{node}", json={}).status_code == 405

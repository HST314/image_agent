import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_core.jobs import JobStore
from storage.project_store import (ActiveJobConflictError, BranchVersionConflictError,
                                   LegacyCheckpointReadOnlyError, ProjectStore, atomic_json,
                                   content_hash)
import main_front


def _project(root: Path, name: str = "p2") -> tuple[ProjectStore, str]:
    store = ProjectStore(root, name)
    store.create({"runtime_policy": {"mode": "offline", "schema_version": 1, "policy": {}}})
    checkpoint = store.checkpoint("confirmation_build", {
        "state": "confirmation_build", "phase": "waiting_task_spec_confirmation",
        "raw_design_task_envelope": {"task_id": "immutable"},
        "task_specification": {"version": 1, "content_hash": "spec"},
        "task_spec_confirmation": {"subject_sha256": "spec"},
        "inspection": {"passed": True}, "final_confirmation": {"asset_sha256": "asset"},
        "delivery_frozen": True,
    })
    return store, checkpoint


def test_lists_current_and_read_only_inspection_never_moves_head(tmp_path: Path):
    store, checkpoint = _project(tmp_path)
    before_manifest = (store.root / "manifest.json").read_bytes()
    before_branches = (store.root / "branches.json").read_bytes()
    listing = store.list_branches()
    viewed = store.inspect_checkpoint(checkpoint)
    assert listing["version"] == 2
    assert [(item["name"], item["current"]) for item in listing["items"]] == [("main", True)]
    assert viewed["checksum"] == store.manifest()["current_checkpoint"]["checksum"]
    assert (store.root / "manifest.json").read_bytes() == before_manifest
    assert (store.root / "branches.json").read_bytes() == before_branches


def test_invalid_cross_project_and_legacy_checkpoint_are_rejected(tmp_path: Path):
    store, _ = _project(tmp_path, "one")
    other, foreign = _project(tmp_path, "two")
    with pytest.raises(FileNotFoundError, match="不属于本工程"):
        store.inspect_checkpoint(str(other.root / foreign))
    with pytest.raises(FileNotFoundError):
        store.inspect_checkpoint("checkpoints/main/missing.json")

    legacy = {"format_version": 1, "branch": "main", "sequence": 9,
              "state": "removed", "data": {"state": "removed"}}
    legacy["checksum"] = content_hash(legacy)
    relative = "checkpoints/main/000009-removed.json"
    atomic_json(store.root / relative, legacy)
    with pytest.raises(LegacyCheckpointReadOnlyError, match="仅允许历史审计"):
        store.inspect_checkpoint(relative)


def test_reopen_creates_child_preserves_source_and_invalidates_branch_scoped_facts(tmp_path: Path):
    store, checkpoint = _project(tmp_path)
    source_bytes = (store.root / checkpoint).read_bytes()
    branch = store.branch_from(checkpoint, name="revision", actor="alice", expected_version=2)
    assert branch == "revision"
    assert (store.root / checkpoint).read_bytes() == source_bytes
    listing = store.list_branches()
    main, child = {item["name"]: item for item in listing["items"]}.values()
    assert child["parent_branch_id"] == main["branch_id"]
    assert child["fork_checkpoint"] == checkpoint and child["created_by"] == "alice" and child["current"]
    reopened = store.resume()
    assert reopened["raw_design_task_envelope"] == {"task_id": "immutable"}
    for forbidden in ("task_spec_confirmation", "inspection", "final_confirmation", "delivery_frozen"):
        assert forbidden not in reopened
    assert store.runtime_snapshot()["mode"] == "offline"
    with pytest.raises(ValueError, match="不能切换"):
        store.assert_runtime_mode("real")


def test_active_job_blocks_move_and_reload_preserves_relationship(tmp_path: Path):
    store, checkpoint = _project(tmp_path)
    jobs = JobStore(store.root)
    jobs.create("active-key", {"action": "advance"})
    with pytest.raises(ActiveJobConflictError, match="活跃作业"):
        store.branch_from(checkpoint, name="blocked", expected_version=2)
    job = jobs.active()[0]
    jobs.cancel(job["job_id"])
    store.branch_from(checkpoint, name="allowed", expected_version=2)
    reloaded = ProjectStore(tmp_path, "p2").list_branches()
    assert len(reloaded["items"]) == 2
    assert next(item for item in reloaded["items"] if item["name"] == "allowed")["current"]


def test_concurrent_switch_with_same_expected_version_has_one_winner(tmp_path: Path):
    store, main_checkpoint = _project(tmp_path)
    store.branch_from(main_checkpoint, name="child", expected_version=2)
    listing = store.list_branches()
    by_name = {item["name"]: item for item in listing["items"]}
    child_checkpoint = by_name["child"]["head"]["path"]
    store.switch_branch(by_name["main"]["branch_id"], main_checkpoint, expected_version=3)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def move(branch_id: str, checkpoint: str) -> None:
        contender = ProjectStore(tmp_path, "p2")
        barrier.wait()
        try:
            contender.switch_branch(branch_id, checkpoint, expected_version=4)
            results.append("ok")
        except BranchVersionConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=move, args=(by_name["child"]["branch_id"], child_checkpoint)),
               threading.Thread(target=move, args=(by_name["main"]["branch_id"], main_checkpoint))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert sorted(results) == ["conflict", "ok"]
    assert ProjectStore(tmp_path, "p2").list_branches()["version"] == 5


def test_http_contract_lists_inspects_reopens_and_reports_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store, checkpoint = _project(tmp_path, "http-p2")
    client = TestClient(main_front.app)
    listed = client.get("/api/projects/http-p2/branches")
    assert listed.status_code == 200 and listed.json()["items"][0]["current"] is True
    branch, filename = checkpoint.split("/")[-2:]
    before = store.manifest()["current_checkpoint"]
    inspected = client.get(f"/api/projects/http-p2/checkpoints/{branch}/{filename}")
    assert inspected.status_code == 200 and store.manifest()["current_checkpoint"] == before
    reopened = client.post("/api/projects/http-p2/branches", json={
        "checkpoint": checkpoint, "name": "http-child", "actor": "api-user", "expected_version": 2})
    assert reopened.status_code == 200
    child = next(item for item in reopened.json()["branches"]["items"] if item["name"] == "http-child")
    stale = client.post("/api/projects/http-p2/branches/switch", json={
        "branch_id": child["branch_id"], "checkpoint": child["head"]["path"], "expected_version": 2})
    assert stale.status_code == 409 and "版本冲突" in stale.json()["detail"]
    missing = client.post("/api/projects/http-p2/branches/switch", json={
        "branch_id": "branch_" + "0" * 32, "checkpoint": checkpoint, "expected_version": 3})
    assert missing.status_code == 404

"""FastAPI 薄适配层的契约与安全测试。"""
from __future__ import annotations

from pathlib import Path
import base64
import json
import multiprocessing
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

import main_front
from storage.assets import normalize_image_asset


def _claim_mkdir_and_crash(projects_root: str, project_id: str, key: str, raw_hash: str) -> None:
    main_front.ProjectStore.claim_design_task(projects_root, project_id, key, raw_hash)
    (Path(projects_root) / project_id).mkdir()
    os._exit(91)


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


def test_envelope_creation_recovers_after_claim_then_create_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = __import__("json").loads(Path("examples/design_task_envelope_v1.valid.json").read_text(encoding="utf-8"))
    envelope["task"]["project_id"] = "crash-recovery"
    original = main_front.ProjectStore.create
    calls = 0

    def crash_once(store, config=None, recovery_claim=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected crash after claim")
        return original(store, config, recovery_claim=recovery_claim)

    monkeypatch.setattr(main_front.ProjectStore, "create", crash_once)
    body = {"project_id": "crash-recovery", "envelope": envelope, "offline": True}
    assert client.post("/api/projects", json=body).status_code == 503
    recovered = client.post("/api/projects", json=body)
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["project_id"] == "crash-recovery"


def test_envelope_http_recovers_empty_directory_left_by_crashed_claim_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = json.loads(Path("examples/design_task_envelope_v1.valid.json").read_text(encoding="utf-8"))
    envelope["task"]["project_id"] = "crash-after-mkdir"
    raw_hash = main_front.content_hash(envelope)
    process = multiprocessing.Process(
        target=_claim_mkdir_and_crash,
        args=(str(main_front.PROJECTS_ROOT), "crash-after-mkdir", envelope["idempotency_key"], raw_hash),
    )
    process.start(); process.join(timeout=10)
    assert process.exitcode == 91

    runs = []
    original_runner = main_front._runner

    def counted_runner(*args, **kwargs):
        runner = original_runner(*args, **kwargs)
        original_run = runner.run
        runner.run = lambda *run_args, **run_kwargs: (runs.append(True), original_run(*run_args, **run_kwargs))[1]
        return runner

    monkeypatch.setattr(main_front, "_runner", counted_runner)
    body = {"project_id": "crash-after-mkdir", "envelope": envelope, "offline": True}
    recovered = client.post("/api/projects", json=body)
    assert recovered.status_code == 201, recovered.text
    repeated = client.post("/api/projects", json=body)
    assert repeated.status_code == 201, repeated.text
    assert len(runs) == 1
    events = main_front.ProjectStore(main_front.PROJECTS_ROOT, "crash-after-mkdir").history()
    assert sum(event["type"] == "project_created" for event in events) == 1


@pytest.mark.parametrize("entry", ["manifest.json", "unknown.bin"])
def test_claim_recovery_never_overwrites_existing_or_unknown_project_data(
    client: TestClient, entry: str
) -> None:
    envelope = json.loads(Path("examples/design_task_envelope_v1.valid.json").read_text(encoding="utf-8"))
    envelope["idempotency_key"] = f"protected-{entry}"
    envelope["task"]["project_id"] = f"protected-{entry.split('.')[0]}"
    project_id = envelope["task"]["project_id"]
    raw_hash = main_front.content_hash(envelope)
    main_front.ProjectStore.claim_design_task(
        main_front.PROJECTS_ROOT, project_id, envelope["idempotency_key"], raw_hash
    )
    root = main_front.PROJECTS_ROOT / project_id
    root.mkdir()
    marker = root / entry
    marker.write_bytes(b"do-not-touch")
    main_front.ProjectStore.abandon_design_task(
        main_front.PROJECTS_ROOT, envelope["idempotency_key"], raw_hash, project_id
    )

    response = client.post(
        "/api/projects", json={"project_id": project_id, "envelope": envelope, "offline": True}
    )
    assert response.status_code == 409
    assert marker.read_bytes() == b"do-not-touch"


def test_different_envelope_cannot_take_over_empty_directory_owned_by_old_claim(
    client: TestClient,
) -> None:
    original = json.loads(Path("examples/design_task_envelope_v1.valid.json").read_text(encoding="utf-8"))
    original["idempotency_key"] = "original-registration"
    original["task"]["project_id"] = "shared-canonical"
    original_hash = main_front.content_hash(original)
    main_front.ProjectStore.claim_design_task(
        main_front.PROJECTS_ROOT, "shared-canonical", original["idempotency_key"], original_hash
    )
    root = main_front.PROJECTS_ROOT / "shared-canonical"
    root.mkdir()
    main_front.ProjectStore.abandon_design_task(
        main_front.PROJECTS_ROOT, original["idempotency_key"], original_hash, "shared-canonical"
    )
    before = list(root.iterdir())

    different = json.loads(json.dumps(original))
    different["idempotency_key"] = "different-registration"
    different["task"]["deliverable_goal"] = "字节不同的新任务不得接管旧登记目录"
    assert main_front.content_hash(different) != original_hash
    response = client.post(
        "/api/projects",
        json={"project_id": "shared-canonical", "envelope": different, "offline": True},
    )

    assert response.status_code == 409
    assert list(root.iterdir()) == before == []


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
    assert confirmed.status_code == 202
    job_id = confirmed.json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/projects/confirm-web/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(.01)
    assert job["status"] == "succeeded"
    fact = client.get("/api/projects/confirm-web").json()["snapshot"]["task_spec_confirmation"]
    assert fact["actor"] == "operator-1" and fact["subject_sha256"]


def test_advance_is_idempotent_async_job_and_mode_is_immutable(client: TestClient) -> None:
    task = {"task_id":"async", "project_id":"async-web", "source_refs":[{"ref_id":"b","ref_type":"brief"}],
            "deliverable_goal":"海报", "usage_context":"审核", "known_facts":{"主体":"产品"},
            "unknowns":{}, "asset_inputs":[], "status":"draft"}
    assert client.post("/api/projects", json={"project_id":"async-web", "task_card":task, "offline":True}).status_code == 201
    payload = {"offline":True, "task_spec_action":"confirm", "actor":"operator", "idempotency_key":"confirm-key-001"}
    responses = []
    def submit():
        responses.append(client.post("/api/projects/async-web/advance", json=payload))
    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert {response.status_code for response in responses} == {202}
    bodies = [response.json() for response in responses]
    assert len({body["job_id"] for body in bodies}) == 1
    assert sum(bool(body["created"]) for body in bodies) == 1
    switched = client.post("/api/projects/async-web/advance", json={"offline":False, "idempotency_key":"switch-key-001"})
    assert switched.status_code == 409


def _quality_disposition_project(project_id: str) -> main_front.ProjectStore:
    store = main_front.ProjectStore(main_front.PROJECTS_ROOT, project_id)
    store.create()
    asset = normalize_image_asset({
        "uri": "https://images.example/quality-limit.png", "provider": "ark", "model": "seedream",
    })
    store.checkpoint("self_check_iteration", {
        "state": "self_check_iteration", "phase": "waiting_quality_disposition", "waiting": True,
        "asset": asset, "current_asset": asset, "round": 2, "quality_cycle": 1,
        "failed_items": ["标题对比度不足"],
        "inspection": {"passed": False, "decision": "continue", "deviations": ["标题对比度不足"],
                       "rework_prompt_delta": "增强标题对比度", "confidence": .8},
        "calibration_status": "waiting_human_disposition", "termination_satisfied": False,
        "termination_reason": "solo_round_limit", "latest_checked_asset_hash": asset["sha256"],
        "selected_policy": {"termination": "solo", "release": "auto", "max_rounds": 2},
        "task_specification": {"task_id": "t", "version": 1, "facts": [], "parent_hash": None,
                               "content_hash": "s"},
    })
    return store


def _wait_for_job(client: TestClient, project_id: str, job_id: str) -> dict:
    for _ in range(200):
        job = client.get(f"/api/projects/{project_id}/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(.01)
    pytest.fail(f"job {job_id} did not finish")


@pytest.mark.parametrize("action,expected_phase", [
    ("continue_generation", "waiting_quality_disposition"),
    ("manual_rework", "waiting_human_rework"),
    ("abandon", "abandoned"),
])
def test_http_quality_disposition_preserves_idempotency_and_checkpoints(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str, expected_phase: str
) -> None:
    monkeypatch.setattr(main_front.JOB_WORKER, "submit", lambda project_id, job_id: main_front._execute_job(
        project_id, {"job_id": job_id}))
    project_id = f"quality-{action.replace('_', '-')}"
    store = _quality_disposition_project(project_id)
    before = store.manifest()["current_checkpoint"]["sequence"]
    payload = {"offline": True, "quality_action": action, "actor": "operator",
               "idempotency_key": f"quality-{action}-001",
               "expense_confirmed": action == "continue_generation"}

    first = client.post(f"/api/projects/{project_id}/advance", json=payload)
    duplicate = client.post(f"/api/projects/{project_id}/advance", json=payload)
    assert first.status_code == duplicate.status_code == 202
    assert first.json()["job_id"] == duplicate.json()["job_id"]
    assert {first.json()["created"], duplicate.json()["created"]} == {True, False}
    assert _wait_for_job(client, project_id, first.json()["job_id"])["status"] == "succeeded"

    view = client.get(f"/api/projects/{project_id}").json()
    assert view["manifest"]["current_checkpoint"]["sequence"] > before
    assert view["snapshot"]["phase"] == expected_phase
    if action != "continue_generation":
        assert view["snapshot"]["quality_disposition"]["idempotency_key"] == payload["idempotency_key"]
    recorded = [event for event in view["history"] if event["type"] == "quality_disposition_recorded"]
    assert len(recorded) == 1 and recorded[0]["idempotency_key"] == payload["idempotency_key"]


def test_http_quality_disposition_missing_idempotency_key_fails_without_checkpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_front.JOB_WORKER, "submit", lambda project_id, job_id: main_front._execute_job(
        project_id, {"job_id": job_id}))
    store = _quality_disposition_project("quality-missing-key")
    before = store.manifest()["current_checkpoint"]
    queued = client.post("/api/projects/quality-missing-key/advance", json={
        "offline": True, "quality_action": "abandon", "actor": "operator",
    })
    assert queued.status_code == 202
    job = _wait_for_job(client, "quality-missing-key", queued.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "INVALID_INPUT"
    assert "idempotency_key" in job["error"]["detail"]
    assert store.manifest()["current_checkpoint"] == before
    assert not any(event["type"] == "quality_disposition_recorded" for event in store.history())


def test_job_cancel_and_event_sequences(client: TestClient, monkeypatch) -> None:
    task = {"task_id":"cancel", "project_id":"cancel-web", "source_refs":[{"ref_id":"b","ref_type":"brief"}],
            "deliverable_goal":"海报", "usage_context":"审核", "known_facts":{"主体":"产品"},
            "unknowns":{}, "asset_inputs":[], "status":"draft"}
    assert client.post("/api/projects", json={"project_id":"cancel-web", "task_card":task, "offline":True}).status_code == 201
    original_submit = main_front.JOB_WORKER.submit
    monkeypatch.setattr(main_front.JOB_WORKER, "submit", lambda *_: None)
    queued = client.post("/api/projects/cancel-web/advance", json={"offline":True, "idempotency_key":"cancel-key-001"})
    cancelled = client.post(f"/api/projects/cancel-web/jobs/{queued.json()['job_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    monkeypatch.setattr(main_front.JOB_WORKER, "submit", original_submit)
    events = main_front._store("cancel-web").history()
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert [event["sequence"] for event in events if event["sequence"] > sequences[-2]] == [sequences[-1]]


def test_http_sse_resumes_after_last_event_id(client: TestClient, monkeypatch) -> None:
    task = {"task_id":"sse", "project_id":"sse-web", "source_refs":[{"ref_id":"b","ref_type":"brief"}],
            "deliverable_goal":"海报", "usage_context":"审核", "known_facts":{"主体":"产品"},
            "unknowns":{}, "asset_inputs":[], "status":"draft"}
    assert client.post("/api/projects", json={"project_id":"sse-web", "task_card":task, "offline":True}).status_code == 201
    store = main_front._store("sse-web")
    before = store.history()[-1]["sequence"]
    resumed = store.events.append("resume_probe", marker="only-new-event")

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(main_front.asyncio, "sleep", no_wait)
    response = client.get("/api/projects/sse-web/events", headers={"Last-Event-ID": str(before)})
    assert response.status_code == 200
    assert f"id: {resumed['sequence']}" in response.text
    assert "only-new-event" in response.text
    assert f"id: {before}\n" not in response.text

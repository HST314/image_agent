import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import validate

import main_front
from storage.project_store import ProjectStore


def audit(state="intake_clarify", role="reasoning_llm", parent=None):
    return {
        "messages": [{"role": "user", "content": "safe prompt"}], "template_id": "t",
        "template_version": "v1", "template_hash": "a" * 64, "variables": {},
        "input_refs": ["artifact_" + "b" * 64],
        "model": {"provider": "fixture", "name": "m", "version": "2026-08", "role": role},
        "parameters": {}, "config_hash": "c" * 64, "state": state, "trace_id": "trace_x",
        "parent_call_id": parent,
    }


def make_store(tmp_path: Path, project="p2trace") -> ProjectStore:
    store = ProjectStore(tmp_path, project)
    store.create()
    return store


def test_text_resume_is_stable_and_final_response_is_authoritative(tmp_path: Path):
    store = make_store(tmp_path)
    call = store.prompts.begin(audit())
    for part in ("one", "-two", "-three"):
        store.prompts.chunk(call, part)
    store.prompts.complete(call, output_raw="one-two-three", output_parsed={"ok": True})
    first = store.prompts.chunks(call, after=0, limit=2)
    second = store.prompts.chunks(call, after=first["next_after"], limit=2)
    assert [x["sequence"] for x in first["items"] + second["items"]] == [1, 2, 3]
    assert "".join(x["delta"] for x in first["items"] + second["items"]) == "one-two-three"
    assert second["complete"] and second["final_response"] == "one-two-three"
    assert ProjectStore(tmp_path, "p2trace").prompts.get(call)["status"] == "completed"


def test_concurrent_text_deltas_have_unique_monotonic_sequences(tmp_path: Path):
    store = make_store(tmp_path)
    call = store.prompts.begin(audit())
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(lambda i: store.prompts.chunk(call, str(i)), range(40)))
    assert sorted(sequences) == list(range(1, 41))
    assert [x["sequence"] for x in store.prompts.get(call)["text_deltas"]] == list(range(1, 41))


def test_parent_repair_chain_image_real_states_and_zero_fake_tokens(tmp_path: Path):
    store = make_store(tmp_path)
    parent = store.prompts.begin(audit(state="self_check_inspection", role="vision_language_model"))
    store.prompts.fail(parent, {"code": "PARSE", "message": "invalid"})
    repair = store.prompts.begin(audit(state="self_check_inspection", role="vision_language_model", parent=parent))
    store.prompts.complete(repair, output_raw={"passed": True}, output_parsed={"passed": True})
    image = store.prompts.begin(audit(state="initial_candidate_generation", role="text_to_image_model", parent=repair))
    for status in ("queued", "running", "provider_completed", "ingested"):
        store.prompts.status(image, status)
    store.prompts.complete(image, output_raw={"artifact_id": "artifact_" + "b" * 64})
    assert store.prompts.summary(repair)["parent_call_id"] == parent
    assert [x["status"] for x in store.prompts.summary(image)["status_events"]] == ["queued", "running", "provider_completed", "ingested"]
    assert store.prompts.chunks(image)["items"] == []


def test_api_permission_project_isolation_cursor_and_deep_redaction(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    one = make_store(tmp_path, "one")
    make_store(tmp_path, "two")
    record = audit()
    record["messages"] = [{"authorization": "Bearer topsecret", "nested": {
        "signed_url": "https://vendor/x?signature=abc", "restricted_content": "private body",
        "path": "/srv/private/project/file.txt"}}]
    call = one.prompts.begin(record)
    one.prompts.complete(call, output_raw={"token": "secret", "body": "Bearer hidden"})
    client = TestClient(main_front.app)
    normal = client.get(f"/api/projects/one/model-calls/{call}")
    assert normal.status_code == 200 and "messages" not in normal.json()["call"]
    assert client.get(f"/api/projects/one/model-calls/{call}?detail=true").status_code == 403
    main_front.app.state.model_call_detail_authorizer = lambda request, project: request.headers.get("x-audit") == "yes"
    detail = client.get(f"/api/projects/one/model-calls/{call}?detail=true", headers={"x-audit": "yes"})
    encoded = json.dumps(detail.json(), ensure_ascii=False)
    assert detail.status_code == 200
    for forbidden in ("topsecret", "signature=abc", "private body", "/srv/private", "Bearer hidden"):
        assert forbidden not in encoded
    assert client.get(f"/api/projects/two/model-calls/{call}").status_code == 404
    assert client.get("/api/projects/one/model-calls/not-real").status_code == 404
    assert client.get("/api/projects/one/model-calls?cursor=bad").status_code == 409
    assert client.get("/api/projects/one/model-calls?limit=101").status_code == 422


def test_summary_schema_read_is_zero_write_and_ui_delete_cannot_delete_audit(tmp_path: Path):
    store = make_store(tmp_path)
    call = store.prompts.begin(audit())
    store.prompts.complete(call, output_raw="done")
    before = store.prompts.path.read_bytes()
    summary = store.prompts.summary(call)
    validate(summary, json.loads(Path("schemas/ModelCallTrace.v1.schema.json").read_text()))
    assert store.prompts.list_calls()["items"][0]["call_id"] == call
    assert store.prompts.path.read_bytes() == before
    assert not hasattr(store.prompts, "delete")

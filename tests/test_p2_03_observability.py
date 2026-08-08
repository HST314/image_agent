from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobStore
from agent_core.observability import MAX_EVENT_PAGE, event_page, progress_projection
from storage.project_store import ProjectStore, atomic_json


def _project(tmp_path: Path, name: str = "observability") -> ProjectStore:
    store = ProjectStore(tmp_path, name)
    store.create()
    return store


def _append_large_log(store: ProjectStore, count: int) -> None:
    with store.events.path.open("a", encoding="utf-8") as stream:
        start = store.events.last_sequence() + 1
        for sequence in range(start, start + count):
            stream.write(json.dumps({"format_version": 1, "event_id": f"event-{sequence}",
                                     "sequence": sequence, "timestamp": "2026-01-01T00:00:00Z",
                                     "type": "tick", "slot": sequence % 5}) + "\n")


def test_stable_pages_since_and_concurrent_append_have_no_gaps_or_duplicates(tmp_path: Path) -> None:
    store = _project(tmp_path)
    for number in range(12):
        store.events.append("step_started", state=f"phase-{number}", trace_id="trace_" + f"{number:032x}")
    first = event_page(store.events, limit=5)
    store.events.append("job_queued", job_id="job_" + "a" * 32)
    second = event_page(store.events, limit=5, cursor=first["next_cursor"])
    third = event_page(store.events, limit=5, cursor=second["next_cursor"])
    frozen = first["items"] + second["items"] + third["items"]
    assert [item["sequence"] for item in frozen] == list(range(1, first["through_sequence"] + 1))
    assert len({item["event_id"] for item in frozen}) == len(frozen)
    incremental = event_page(store.events, since=first["through_sequence"])
    assert [item["sequence"] for item in incremental["items"]] == [first["through_sequence"] + 1]


def test_event_bounds_bad_cursor_and_streaming_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _project(tmp_path)
    for number in range(250):
        store.events.append("tick", slot=number % 5)
    monkeypatch.setattr(store.events, "read_all", lambda: (_ for _ in ()).throw(AssertionError("full load")))
    assert len(event_page(store.events, limit=MAX_EVENT_PAGE)["items"]) == MAX_EVENT_PAGE
    with pytest.raises(ValueError, match="分页大小"):
        event_page(store.events, limit=MAX_EVENT_PAGE + 1)
    with pytest.raises(ValueError, match="游标"):
        event_page(store.events, cursor="not-a-cursor")
    with pytest.raises(ValueError, match="不能同时"):
        event_page(store.events, cursor="x", since=1)
    with pytest.raises(ValueError, match="since"):
        event_page(store.events, since=9999)


def test_large_frozen_window_is_read_completely_by_cursor_without_gaps(tmp_path: Path) -> None:
    store = _project(tmp_path)
    _append_large_log(store, 10_001)
    first = event_page(store.events, limit=100)
    store.events.append("concurrent_append")
    pages = [first]
    while pages[-1]["next_cursor"]:
        pages.append(event_page(store.events, limit=100, cursor=pages[-1]["next_cursor"]))
    sequences = [item["sequence"] for page in pages for item in page["items"]]
    assert sequences == list(range(1, first["through_sequence"] + 1))
    assert len(sequences) == len(set(sequences))
    assert store.events.last_sequence() == first["through_sequence"] + 1


def test_large_stale_since_catches_up_in_bounded_pages(tmp_path: Path) -> None:
    store = _project(tmp_path)
    _append_large_log(store, 10_001)
    page = event_page(store.events, limit=100, since=1)
    sequences: list[int] = []
    page_sizes: list[int] = []
    while True:
        sequences.extend(item["sequence"] for item in page["items"])
        page_sizes.append(len(page["items"]))
        if not page["next_cursor"]:
            break
        page = event_page(store.events, limit=100, cursor=page["next_cursor"])
    assert sequences == list(range(2, store.events.last_sequence() + 1))
    assert max(page_sizes) == 100


def test_sse_stale_resume_yields_bounded_backlog_without_stream_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _project(tmp_path, "sse-large")
    _append_large_log(store, 10_001)
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)

    class RequestStub:
        headers = {"last-event-id": "1"}

        async def is_disconnected(self) -> bool:
            return False

    async def consume_first_batch() -> list[str]:
        response = await main_front.project_events("sse-large", RequestStub(), after=0)
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
            if len(chunks) == 100:
                break
        return chunks

    chunks = asyncio.run(consume_first_batch())
    assert len(chunks) == 100
    assert chunks[0].startswith("id: 2\n")
    assert chunks[-1].startswith("id: 101\n")
    assert not any("observability_error" in chunk for chunk in chunks)


def test_public_dto_is_allowlisted_and_nested_secrets_payloads_paths_and_pii_never_escape(tmp_path: Path) -> None:
    store = _project(tmp_path)
    secret_samples = {
        "api_key": "sk-super-secret", "token": "bearer-secret",
        "person": {"name": "Alice", "email": "alice@example.com", "phone": "13800138000"},
        "provider_raw_response": {"signed_url": "https://vendor/x?signature=secret", "body": "raw"},
        "local_path": "/srv/private/projects/customer/file.png",
        "error": {"code": "UPSTREAM_TIMEOUT", "detail": "Authorization: Bearer hidden /etc/passwd",
                  "trace_id": "trace_" + "b" * 32},
    }
    store.events.append("candidate_failed", state="five_candidate_generation", index=2, attempt=3,
                        **secret_samples)
    result = event_page(store.events)
    encoded = json.dumps(result, ensure_ascii=False)
    for forbidden in ("sk-super-secret", "bearer-secret", "Alice", "alice@example.com", "13800138000",
                      "vendor", "signature", "/srv/", "/etc/passwd", "Authorization"):
        assert forbidden not in encoded
    item = result["items"][-1]
    assert item == {"event_id": item["event_id"], "sequence": 2, "event_type": "candidate_failed",
                    "phase": "five_candidate_generation", "job_id": None, "slot": 2, "round": None,
                    "status": "failed", "timestamp": item["timestamp"], "trace_id": "trace_" + "b" * 32,
                    "retry_count": 2, "error_code": "UPSTREAM_TIMEOUT", "message": None}


def test_progress_partial_retry_quality_limit_cancel_recovery_and_waiting(tmp_path: Path) -> None:
    store = _project(tmp_path)
    jobs = JobStore(store.root)
    job, _ = jobs.create("progress-key", {"mode": "offline"})
    jobs.claim(job["job_id"])
    jobs.heartbeat(job["job_id"], completed=3, total=5, unit="candidate")
    for slot in (0, 1, 2):
        store.events.append("candidate_succeeded", index=slot, attempt=1)
    store.events.append("candidate_failed", index=3, attempt=2,
                        error={"code": "RATE_LIMITED", "trace_id": "trace_" + "c" * 32})
    store.events.append("inspection_presented", round=4)
    pointer = store.manifest()["current_checkpoint"]
    assert pointer is None
    # A direct immutable checkpoint fixture represents a normal human wait.
    relative, checksum = store.checkpoints.save("main", 1, "self_check_iteration", {
        "phase": "waiting_quality_disposition", "calibration_status": "waiting_human_disposition"
    })
    manifest_path = store.root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["current_checkpoint"] = {"path": relative, "checksum": checksum, "branch": "main",
                                      "sequence": 1, "state": "self_check_iteration"}
    atomic_json(manifest_path, manifest)
    before = {path.relative_to(store.root).as_posix(): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    projected = progress_projection(store.root, store.events)
    after = {path.relative_to(store.root).as_posix(): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    assert before == after
    assert projected["status"] == "waiting" and projected["waiting_for_human"] is True
    assert projected["work"] == {"completed": 3, "total": 5, "unit": "candidate"}
    assert projected["candidates"] == {"completed_slots": [0, 1, 2], "failed_slots": [3], "total": 5}
    assert projected["quality"] == {"current_round": 4, "max_rounds": 4, "at_limit": True}
    jobs.cancel(job["job_id"]); jobs.finish(job["job_id"])
    assert progress_projection(store.root, store.events)["job"]["status"] == "cancelled"
    assert progress_projection(ProjectStore(tmp_path, store.project_id).root,
                               ProjectStore(tmp_path, store.project_id).events) == progress_projection(store.root, store.events)


def test_http_contract_schema_and_reads_are_byte_for_byte_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path)
    store = _project(tmp_path, "http-observe")
    store.events.append("job_started", job_id="job_" + "d" * 32, attempt=2)
    before = {path.relative_to(store.root).as_posix(): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    client = TestClient(main_front.app)
    page = client.get("/api/projects/http-observe/event-log", params={"limit": 1}).json()
    progress = client.get("/api/projects/http-observe/progress").json()
    after = {path.relative_to(store.root).as_posix(): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    assert before == after and progress["through_sequence"] == 2
    schema = json.loads(Path("schemas/ProjectObservability.v1.schema.json").read_text())
    page_schema = {**schema["$defs"]["event_page"], "$defs": schema["$defs"]}
    jsonschema.Draft202012Validator(page_schema, format_checker=jsonschema.FormatChecker()).validate(page)
    jsonschema.Draft202012Validator(schema["$defs"]["progress"]).validate(progress)
    assert client.get("/api/projects/http-observe/event-log", params={"limit": 101}).status_code == 422
    assert client.get("/api/projects/http-observe/event-log", params={"cursor": "bad"}).status_code == 409

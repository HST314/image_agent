from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path

from agent_core.jobs import JobStore
from agent_core.batch import CandidateBatchGenerator
from storage.project_store import ProjectStore
import jsonschema


def _claim_and_crash(project_root: str, job_id: str) -> None:
    jobs = JobStore(Path(project_root))
    assert jobs.claim(job_id)["status"] == "running"
    os._exit(91)


def test_running_job_is_recovered_after_worker_process_crash(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "recover-job")
    store.create()
    jobs = JobStore(store.root)
    job, _ = jobs.create("recover-key-001", {"options": {"offline": True}, "mode": "offline"})
    process = mp.Process(target=_claim_and_crash, args=(str(store.root), job["job_id"]))
    process.start(); process.join(5)
    assert process.exitcode == 91
    assert jobs.recoverable() == [job["job_id"]]
    reclaimed = jobs.claim(job["job_id"])
    assert reclaimed["attempt"] == 2 and reclaimed["status"] == "running"


def test_failed_retry_reuses_same_job_id_and_attempt(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "retry-job")
    store.create()
    jobs = JobStore(store.root)
    first, _ = jobs.create("retry-key-001", {"options": {"offline": True}, "mode": "offline"})
    jobs.claim(first["job_id"])
    jobs.finish(first["job_id"], error={"code": "Timeout", "message": "timeout", "retryable": True})
    retried, created = jobs.create("retry-key-001", {"options": {"offline": True}, "mode": "offline"})
    assert not created and retried["job_id"] == first["job_id"] and retried["status"] == "queued"
    assert jobs.claim(first["job_id"])["attempt"] == 2


def test_async_job_schema_accepts_persisted_status(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "schema-job")
    store.create()
    job, _ = JobStore(store.root).create("schema-key-001", {"options": {}, "mode": "offline"})
    schema = json.loads(Path("schemas/AsyncJob.v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(job)


def test_failed_candidate_retry_reuses_slot_key_and_only_pays_failed_slot(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path, "slots")
    store.create()
    calls: list[int] = []
    fail_once = {2}
    def render(index: int):
        calls.append(index)
        if index in fail_once:
            fail_once.remove(index)
            raise RuntimeError("crash after provider timeout")
        return {"uri": str(index), "sha256": str(index), "candidate_index": index}
    generator = CandidateBatchGenerator(store, render, attempts=1, max_workers=5)
    first = generator.generate("same-confirmed-spec", count=5)
    second = generator.generate("same-confirmed-spec", count=5)
    assert [item["index"] for item in first["failed"]] == [2]
    assert not second["failed"] and len(second["succeeded"]) == 5
    assert calls.count(2) == 2
    assert all(calls.count(index) == 1 for index in (0, 1, 3, 4))
    keys = [event["idempotency_key"] for event in store.history()
            if event["type"] in {"candidate_failed", "candidate_succeeded"} and event["index"] == 2]
    assert len(set(keys)) == 1

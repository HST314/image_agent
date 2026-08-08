from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_core.batch import CandidateBatchGenerator
from agent_core.error_taxonomy import JobCancelledError, error_record
from agent_core.jobs import JobStore
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from model_router.executor import ModelCallError, ModelExecutor
from storage.project_store import ProjectStore


class HttpError(RuntimeError):
    def __init__(self, status_code: int, message: str, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@pytest.mark.parametrize(
    "exc,code,retryable,action",
    [
        (TimeoutError("slow"), "UPSTREAM_TIMEOUT", True, "retry"),
        (HttpError(429, "limited", 7), "RATE_LIMITED", True, "retry"),
        (HttpError(401, "bad credential"), "AUTHENTICATION_FAILED", False, "contact_admin"),
        (HttpError(403, "content policy rejected"), "CONTENT_REJECTED", False, "modify_input"),
        (ConnectionError("network"), "PROVIDER_UNAVAILABLE", True, "retry"),
        (type("AssetIngestionError", (RuntimeError,), {})("disk"), "ASSET_INGESTION_FAILED", True, "retry"),
        (type("StructuredOutputError", (RuntimeError,), {})("bad json"), "STRUCTURED_OUTPUT_INVALID", True, "retry"),
        (ValueError("bad input"), "INVALID_INPUT", False, "modify_input"),
        (type("SkillLoadError", (RuntimeError,), {})("missing"), "CONFIGURATION_OR_SKILL", False, "contact_admin"),
        (type("JobCancelledError", (RuntimeError,), {})("stop"), "CANCELLED", False, "none"),
    ],
)
def test_stable_taxonomy(exc, code, retryable, action):
    record = error_record(exc, stage="render", slot=2, rework_round=3, trace_id="trace_" + "a" * 32)
    assert (record["code"], record["retryable"], record["suggested_action"]) == (code, retryable, action)
    assert record["candidate_slot"] == 2 and record["rework_round"] == 3
    assert record["trace_id"] == "trace_" + "a" * 32


def test_runner_cancellation_message_is_not_misclassified_as_internal_error():
    record = error_record(RuntimeError("作业已请求取消，未开始的供应商调用已停止。"),
                          stage="initial_candidate_generation")
    assert record["code"] == "CANCELLED"
    assert record["retryable"] is False
    assert record["suggested_action"] == "none"


def test_runner_cancel_before_handler_writes_no_failure_or_checkpoint(tmp_path: Path):
    store = ProjectStore(tmp_path, "cancel-before-provider"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True,
                            should_cancel=lambda: True)
    called = []
    runner.handlers["initial_candidate_generation"] = lambda *_: called.append(True) or {}

    with pytest.raises(JobCancelledError):
        runner.run({"state": "confirmation_build"}, RunnerOptions(),
                   only_state="initial_candidate_generation")

    assert called == []
    assert store.manifest().get("failed_step") is None
    assert not [event for event in store.history() if event["type"] in {"step_failed", "step_succeeded"}]


def test_runner_cancel_after_handler_skips_success_and_error_checkpoints(tmp_path: Path):
    store = ProjectStore(tmp_path, "cancel-before-checkpoint"); store.create()
    cancelled = False
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True,
                            should_cancel=lambda: cancelled)

    def handler(*_):
        nonlocal cancelled
        cancelled = True
        return {"phase": "candidate_generation_completed"}

    runner.handlers["initial_candidate_generation"] = handler
    with pytest.raises(JobCancelledError):
        runner.run({"state": "confirmation_build"}, RunnerOptions(),
                   only_state="initial_candidate_generation")

    assert store.manifest().get("failed_step") is None
    assert [event["type"] for event in store.history()].count("step_started") == 1
    assert not [event for event in store.history() if event["type"] in {"step_failed", "step_succeeded"}]


def test_cancel_wins_job_finish_and_refresh_has_neutral_action(tmp_path: Path):
    store = ProjectStore(tmp_path, "cancel-job-finish"); store.create()
    jobs = JobStore(store.root)
    job, _ = jobs.create("cancel-finish-key", {"x": 1})
    jobs.claim(job["job_id"])
    jobs.cancel(job["job_id"])
    finished = jobs.finish(job["job_id"], error=error_record(
        JobCancelledError("作业在完成前收到取消请求。"), stage="workflow"))

    assert finished["status"] == "cancelled"
    assert finished.get("error") is None
    refreshed = jobs.get(job["job_id"])
    assert refreshed["status"] == "cancelled"
    assert refreshed.get("error") is None


def test_retry_after_and_exponential_policy_are_bounded():
    sleeps = []
    calls = 0

    def call():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HttpError(429, "limited", 7)
        return "ok"

    executor = ModelExecutor(max_attempts=3, base_delay=1, max_delay=5, timeout=1,
                             sleeper=sleeps.append, randomizer=lambda _a, _b: 0)
    assert executor.run(call) == "ok"
    assert calls == 3 and sleeps == [5, 5]


def test_non_retryable_401_has_zero_blind_retries():
    calls = 0

    def call():
        nonlocal calls
        calls += 1
        raise HttpError(401, "no")

    with pytest.raises(ModelCallError) as caught:
        ModelExecutor(max_attempts=5, sleeper=lambda _: None).run(call)
    assert calls == 1 and caught.value.retryable is False


def test_secret_and_full_provider_body_are_redacted_and_truncated():
    record = error_record(RuntimeError("Authorization: Bearer abc.def token=xyz " + "P" * 1000), stage="provider")
    assert "abc.def" not in record["detail"] and "xyz" not in record["detail"]
    assert "[REDACTED]" in record["detail"] and len(record["detail"]) <= 513


def test_partial_success_restart_cannot_bypass_exhausted_slot_budget(tmp_path: Path):
    store = ProjectStore(tmp_path, "partial"); store.create()
    calls = []

    def first(index):
        calls.append(index)
        if index == 1:
            raise ConnectionError("down")
        return {"candidate_index": index, "uri": str(index), "sha256": str(index)}

    initial = CandidateBatchGenerator(store, first, attempts=2, max_workers=1).generate("input", count=3)
    failed_key = next(e["idempotency_key"] for e in store.events.read_all() if e["type"] == "candidate_failed")
    assert calls == [0, 1, 1, 2]
    calls.clear()
    recovered = CandidateBatchGenerator(ProjectStore(tmp_path, "partial"),
        lambda index: calls.append(index) or {"candidate_index": index, "uri": str(index), "sha256": str(index)}, attempts=2
    ).generate("input", count=3)
    assert calls == [] and len(recovered["succeeded"]) == 2
    assert [item["index"] for item in recovered["failed"]] == [1]
    events = store.events.read_all()
    assert [e["idempotency_key"] for e in events if e["type"] == "candidate_failed"] == [failed_key, failed_key]
    assert failed_key not in [e["idempotency_key"] for e in events if e["type"] == "candidate_succeeded"]


def test_job_retry_reuses_job_and_stops_at_runtime_limit(tmp_path: Path):
    store = ProjectStore(tmp_path, "jobs"); store.create()
    jobs = JobStore(store.root, max_attempts=2)
    job, _ = jobs.create("stable-key", {"x": 1})
    jobs.claim(job["job_id"])
    jobs.finish(job["job_id"], error=error_record(ConnectionError("x"), stage="render"))
    same, created = jobs.create("stable-key", {"x": 1})
    assert not created and same["job_id"] == job["job_id"] and same["status"] == "queued"
    jobs.claim(job["job_id"])
    jobs.finish(job["job_id"], error=error_record(ConnectionError("x"), stage="render"))
    stopped, _ = jobs.create("stable-key", {"x": 1})
    assert stopped["status"] == "failed" and stopped["attempt"] == 2


def test_error_contract_schema_and_waiting_has_no_error():
    schema = json.loads(Path("schemas/AsyncJob.v1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    error = error_record(HttpError(429, "limited", 4), stage="render", slot=0)
    job = {"job_id": "job_" + "a" * 32, "idempotency_key": "abcdefgh", "status": "failed",
           "progress": {"completed": 0, "total": 5, "unit": "candidate"}, "heartbeat_at": None,
           "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
           "attempt": 1, "max_attempts": 3, "error": error}
    assert list(validator.iter_errors(job)) == []
    waiting_checkpoint = {"phase": "waiting_final_confirmation", "waiting": True}
    assert "error" not in waiting_checkpoint and "retryable" not in waiting_checkpoint

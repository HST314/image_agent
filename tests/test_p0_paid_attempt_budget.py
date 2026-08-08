from __future__ import annotations

import threading

from agent_core.batch import CandidateBatchGenerator
from agent_core.error_taxonomy import error_record
from model_router.executor import ModelCallError, ModelExecutor
from storage.project_store import ProjectStore


def test_executor_timeout_is_unresolved_and_never_blindly_retries() -> None:
    release = threading.Event()
    calls = 0

    def provider() -> str:
        nonlocal calls
        calls += 1
        release.wait(1)
        return "late result"

    executor = ModelExecutor(max_attempts=3, timeout=.01, sleeper=lambda _: None)
    try:
        executor.run(provider)
    except ModelCallError as exc:
        assert exc.category == "provider_status_unknown"
        assert exc.retryable is False
        assert error_record(exc, stage="render")["code"] == "PROVIDER_STATUS_UNKNOWN"
    else:
        raise AssertionError("timeout must not be reported as success")
    finally:
        release.set()
    assert calls == 1


def test_paid_attempt_budget_is_persistent_across_batch_recovery(tmp_path) -> None:
    store = ProjectStore(tmp_path, "budget")
    store.create()
    calls = 0

    def provider(_index: int) -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise ConnectionError("temporary")

    first = CandidateBatchGenerator(store, provider, attempts=2).generate("input", count=1)
    second = CandidateBatchGenerator(ProjectStore(tmp_path, "budget"), provider, attempts=2).generate("input", count=1)

    assert len(first["failed"]) == len(second["failed"]) == 1
    assert calls == 2
    assert second["failed"][0]["error"]["code"] == "PAID_ATTEMPT_BUDGET_EXHAUSTED"
    started = [event for event in store.history() if event["type"] == "candidate_attempt_started"]
    assert [event["attempt"] for event in started] == [1, 2]


def test_unresolved_provider_status_blocks_recovery_payment(tmp_path) -> None:
    store = ProjectStore(tmp_path, "unresolved")
    store.create()
    calls = 0

    def provider(_index: int) -> dict[str, str]:
        nonlocal calls
        calls += 1
        error = ModelCallError("local timeout", False, "provider_status_unknown", "req", "trace")
        raise error

    first = CandidateBatchGenerator(store, provider, attempts=3).generate("input", count=1)
    second = CandidateBatchGenerator(ProjectStore(tmp_path, "unresolved"), provider, attempts=3).generate("input", count=1)

    assert calls == 1
    assert first["failed"][0]["error"]["code"] == "PROVIDER_STATUS_UNKNOWN"
    assert second["failed"][0]["error"]["code"] == "PROVIDER_STATUS_UNKNOWN"
    assert len([event for event in store.history() if event["type"] == "candidate_attempt_unresolved"]) == 1

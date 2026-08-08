"""Five-candidate generation with partial success and idempotent retries."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from storage.project_store import ProjectStore, content_hash
from agent_core.error_taxonomy import JobCancelledError, error_record

class PaidAttemptBudgetExceeded(RuntimeError):
    """A slot has consumed every durable paid provider attempt."""

class CandidateBatchGenerator:
    def __init__(self, store: ProjectStore, render: Callable[[int], dict[str, Any]], *, attempts: int = 2, max_workers: int = 5,
                 recover: Callable[[int, str], dict[str, Any] | None] | None = None,
                 should_cancel: Callable[[], bool] | None = None,
                 on_progress: Callable[[int, int], None] | None = None) -> None:
        self.store, self.render, self.attempts = store, render, attempts
        self.max_workers = max(1, min(5, max_workers))
        self.recover = recover
        self.should_cancel = should_cancel or (lambda: False)
        self.on_progress = on_progress or (lambda _completed, _total: None)

    def generate(self, input_hash: str, *, count: int = 5, slot_identities: list[str] | None = None) -> dict[str, list[Any]]:
        if slot_identities is not None and len(slot_identities) != count:
            raise ValueError("slot_identities must match candidate count")
        successes: list[Any] = []; failures: list[Any] = []
        with self.store.lock():
            events = self.store.events.read_all()
            pending: list[tuple[int, str]] = []
            for index in range(count):
                identity = slot_identities[index] if slot_identities is not None else str(index)
                key = content_hash(["initial_candidate_generation", input_hash, index, identity])
                cached = next((e.get("asset") for e in reversed(events) if e.get("type") == "candidate_succeeded" and e.get("idempotency_key") == key), None)
                if cached: successes.append(cached); continue
                unresolved = next((e for e in reversed(events)
                                   if e.get("type") == "candidate_attempt_unresolved"
                                   and e.get("idempotency_key") == key), None)
                if unresolved:
                    failures.append({"index": index, "error": unresolved["error"],
                                     "idempotency_key": key})
                    continue
                pending.append((index, key))
            self.on_progress(len(successes), count)

            def one(index: int, key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
                error: Exception | None = None
                keyed_events = [event for event in events if event.get("idempotency_key") == key]
                first_started = next((position for position, event in enumerate(keyed_events)
                                      if event.get("type") == "candidate_attempt_started"), len(keyed_events))
                # Before attempt-start auditing existed, each failure represented
                # one paid call. From the first start event onward, starts are the
                # sole accounting source so their matching failures are not counted
                # twice and a start followed by a crash still consumes the budget.
                legacy_failures = sum(1 for event in keyed_events[:first_started]
                                      if event.get("type") == "candidate_failed")
                audited_starts = sum(1 for event in keyed_events[first_started:]
                                     if event.get("type") == "candidate_attempt_started")
                prior_started = legacy_failures + audited_starts

                def recover_completed_result() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
                    if self.recover is None:
                        return None, None
                    try:
                        recovered = self.recover(index, key)
                        if recovered is None:
                            return None, None
                        self.store.events.append("candidate_succeeded", index=index, attempt=prior_started,
                                                 asset=recovered, idempotency_key=key, recovered=True)
                        return recovered, None
                    except Exception as exc:
                        record = error_record(exc, stage="asset_ingestion_recovery", slot=index)
                        self.store.events.append("candidate_ingestion_failed", index=index, attempt=prior_started,
                                                 error=record, idempotency_key=key)
                        return None, {"index": index, "error": record, "idempotency_key": key}

                # Recovery is not a paid attempt.  Probe before evaluating the
                # remaining provider budget so the final paid result can still be
                # ingested (or rebound) after a restart.
                if self.should_cancel():
                    raise JobCancelledError("作业已请求取消，恢复探针与供应商调用均未开始。")
                recovered, recovery_failure = recover_completed_result()
                if recovered is not None or recovery_failure is not None:
                    return recovered, recovery_failure
                for attempt in range(prior_started + 1, self.attempts + 1):
                    if self.should_cancel():
                        raise JobCancelledError("作业已请求取消，未开始的供应商调用已停止。")
                    if attempt > prior_started + 1:
                        recovered, recovery_failure = recover_completed_result()
                        if recovered is not None or recovery_failure is not None:
                            return recovered, recovery_failure
                    try:
                        self.store.events.append("candidate_attempt_started", index=index, attempt=attempt,
                                                 idempotency_key=key)
                        asset = self.render(index)
                        self.store.events.append("candidate_succeeded", index=index, attempt=attempt, asset=asset, idempotency_key=key)
                        return asset, None
                    except JobCancelledError:
                        raise
                    except Exception as exc:
                        error = exc
                        record = error_record(exc, stage="five_candidate_generation", slot=index)
                        self.store.events.append("candidate_failed", index=index, attempt=attempt, error=record, idempotency_key=key)
                        if record["code"] == "PROVIDER_STATUS_UNKNOWN":
                            self.store.events.append("candidate_attempt_unresolved", index=index, attempt=attempt,
                                                     error=record, idempotency_key=key)
                        if not record["retryable"]:
                            break
                if error is None:
                    error = PaidAttemptBudgetExceeded("候选槽位的持久化付费尝试预算已耗尽。")
                return None, {"index": index, "error": error_record(error or RuntimeError("unknown"), stage="five_candidate_generation", slot=index), "idempotency_key": key}

            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="candidate") as pool:
                futures = {pool.submit(one, index, key): index for index, key in pending}
                for future in as_completed(futures):
                    asset, failure = future.result()
                    if asset is not None: successes.append(asset)
                    if failure is not None: failures.append(failure)
                    self.on_progress(len(successes), count)
            successes.sort(key=lambda item: int(item.get("candidate_index", item.get("uri", 0))) if str(item.get("candidate_index", item.get("uri", 0))).isdigit() else 0)
            failures.sort(key=lambda item: item["index"])
        return {"succeeded": successes, "failed": failures}

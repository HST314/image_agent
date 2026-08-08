from __future__ import annotations

import io

import pytest
from PIL import Image

from agent_core.batch import CandidateBatchGenerator
from agent_core.workflow_runner import WorkflowRunner
from storage.assets import AssetPersistenceError, persist_image_asset
from storage.project_store import ProjectStore, content_hash


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    return stream.getvalue()


def _record_completed(store: ProjectStore, key: str, result: dict[str, str]) -> str:
    call_id = store.prompts.begin({
        "messages": [{"role": "user", "content": "render"}], "template_id": "initial_candidate_generation",
        "template_version": "2", "template_hash": "hash", "variables": {"idempotency_key": key},
        "input_refs": [], "model": {"provider": "test", "name": "image", "role": "text_to_image_model"},
        "parameters": {}, "config_hash": "config", "state": "initial_candidate_generation", "trace_id": "trace",
    })
    store.prompts.status(call_id, "provider_completed")
    store.prompts.complete(call_id, output_raw=result)
    return call_id


@pytest.mark.parametrize("failure", ["download", "mime", "write"])
def test_completed_result_ingestion_failure_never_repeats_paid_render(tmp_path, monkeypatch, failure: str) -> None:
    project_id = f"recover-{failure}"
    store = ProjectStore(tmp_path, project_id)
    store.create()
    calls = 0
    call_id = ""
    png = _png()

    def provider(_index: int) -> dict[str, str]:
        nonlocal calls, call_id
        calls += 1
        key = content_hash(["initial_candidate_generation", "input", 0, "0"])
        call_id = _record_completed(store, key, {"url": "https://provider.example/result.png", "provider": "test", "model": "image"})
        raise AssetPersistenceError("initial controlled ingestion failed")

    recovery_round = 0

    def recover(_index: int, key: str):
        nonlocal recovery_round
        pending = store.prompts.pending_provider_result(state="initial_candidate_generation", idempotency_key=key)
        if pending is None:
            return None
        pending_call_id, result = pending
        recovery_round += 1
        if recovery_round == 1 and failure == "download":
            fetcher = lambda _url: (_ for _ in ()).throw(AssetPersistenceError("download failed"))
        elif recovery_round == 1 and failure == "mime":
            fetcher = lambda _url: (png, "text/plain")
        else:
            fetcher = lambda _url: (png, "image/png")
        if recovery_round == 1 and failure == "write":
            monkeypatch.setattr(store.artifacts, "save_bytes", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        asset = persist_image_asset(result, store.artifacts, fetcher=fetcher)
        store.prompts.status(pending_call_id, "ingested", artifact_id=asset["artifact_id"], sha256=asset["sha256"])
        return asset

    first = CandidateBatchGenerator(store, provider, attempts=3, recover=recover).generate("input", count=1)
    if failure == "write":
        monkeypatch.undo()
    second_store = ProjectStore(tmp_path, project_id)
    store = second_store
    second = CandidateBatchGenerator(second_store, provider, attempts=3, recover=recover).generate("input", count=1)

    assert first["failed"][0]["error"]["code"] == "ASSET_INGESTION_FAILED"
    assert len(second["succeeded"]) == 1
    assert calls == 1
    assert len([event for event in second_store.history() if event["type"] == "candidate_attempt_started"]) == 1
    assert [event["status"] for event in second_store.prompts.get(call_id)["status_events"]][-1] == "ingested"


def test_recorded_but_unrecoverable_completed_result_blocks_new_payment(tmp_path) -> None:
    store = ProjectStore(tmp_path, "unrecoverable")
    store.create()
    key = content_hash(["initial_candidate_generation", "input", 0, "0"])
    _record_completed(store, key, {"url": "[REDACTED]", "provider": "test", "model": "image"})
    calls = 0

    def provider(_index: int):
        nonlocal calls
        calls += 1
        return {}

    def recover(_index: int, candidate_key: str):
        return store.prompts.pending_provider_result(state="initial_candidate_generation", idempotency_key=candidate_key)

    result = CandidateBatchGenerator(store, provider, attempts=3, recover=recover).generate("input", count=1)

    assert result["failed"][0]["error"]["code"] == "ASSET_INGESTION_FAILED"
    assert calls == 0
    assert not [event for event in store.history() if event["type"] == "candidate_attempt_started"]


def test_completed_result_recovers_even_when_paid_budget_is_exhausted(tmp_path) -> None:
    store = ProjectStore(tmp_path, "exhausted-recovery")
    store.create()
    key = content_hash(["initial_candidate_generation", "input", 0, "0"])
    for attempt in range(1, 4):
        store.events.append("candidate_attempt_started", index=0, attempt=attempt, idempotency_key=key)
    recover_calls = 0
    provider_calls = 0

    def recover(_index: int, candidate_key: str):
        nonlocal recover_calls
        recover_calls += 1
        assert candidate_key == key
        return {"uri": "artifact://artifact_" + "a" * 64, "sha256": "a" * 64}

    def provider(_index: int):
        nonlocal provider_calls
        provider_calls += 1
        return {}

    result = CandidateBatchGenerator(store, provider, attempts=3, recover=recover).generate("input", count=1)

    assert len(result["succeeded"]) == 1
    assert recover_calls == 1
    assert provider_calls == 0


def test_ingested_asset_is_rebound_after_crash_without_provider_call(tmp_path) -> None:
    project_id = "ingested-binding-crash"
    store = ProjectStore(tmp_path, project_id)
    store.create()
    key = content_hash(["initial_candidate_generation", "input", 0, "STYLE-000"])
    call_id = _record_completed(
        store, key, {"url": "https://provider.example/result.png", "provider": "test", "model": "image"}
    )
    saved = store.artifacts.save_bytes(
        _png(), suffix=".png", metadata={"media_type": "image/png", "provider": "test", "model": "image", "mock": False}
    )
    # Simulate the exact crash window: controlled ingestion was audited, but no
    # candidate_succeeded event containing the complete style binding was written.
    store.prompts.status(call_id, "ingested", artifact_id=saved["artifact_id"], sha256=saved["sha256"])
    store.events.append("candidate_attempt_started", index=0, attempt=1, idempotency_key=key)

    restarted = ProjectStore(tmp_path, project_id)
    runner = object.__new__(WorkflowRunner)
    runner.store = restarted
    provider_calls = 0
    audit = {"slot": 0, "style_index": "STYLE-000", "prompt_sha256": "prompt-hash",
             "render_idempotency_key": key}

    def recover(_index: int, candidate_key: str):
        asset = runner._recover_image_call("initial_candidate_generation", candidate_key)
        return {**asset, "candidate_index": 0, "id": "candidate-1", "style_name": "方向 1",
                "style_slot_audit": audit} if asset is not None else None

    def provider(_index: int):
        nonlocal provider_calls
        provider_calls += 1
        return {}

    result = CandidateBatchGenerator(
        restarted, provider, attempts=3, recover=recover
    ).generate("input", count=1, slot_identities=["STYLE-000"])

    assert provider_calls == 0
    assert result["succeeded"][0]["artifact_id"] == saved["artifact_id"]
    assert result["succeeded"][0]["style_slot_audit"] == audit
    rebound = [event for event in restarted.history() if event["type"] == "candidate_succeeded"]
    assert len(rebound) == 1 and rebound[0]["asset"]["style_slot_audit"] == audit

from __future__ import annotations

import io

import pytest
from PIL import Image

from agent_core.batch import CandidateBatchGenerator
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

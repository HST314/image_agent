import json
from pathlib import Path

import pytest

from storage.project_store import ProjectStore


def test_checkpoint_wal_recovers_crash_between_checkpoint_and_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = ProjectStore(tmp_path, "txn")
    store.create()
    original_save = store.checkpoints.save
    def save_then_crash(*args, **kwargs):
        original_save(*args, **kwargs)
        raise SystemExit(91)
    monkeypatch.setattr(store.checkpoints, "save", save_then_crash)
    with pytest.raises(SystemExit):
        store.checkpoint("received", {"value": 1})
    assert (store.root / "runtime/checkpoint-commit.json").exists()

    monkeypatch.setattr(store.checkpoints, "save", original_save)
    assert store.resume() == {"value": 1}
    succeeded = [event for event in store.history() if event["type"] == "step_succeeded"]
    assert len(succeeded) == 1 and succeeded[0]["state"] == "received"
    assert not (store.root / "runtime/checkpoint-commit.json").exists()


def test_checkpoint_wal_rolls_back_intent_when_checkpoint_never_landed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = ProjectStore(tmp_path, "intent-only"); store.create()
    monkeypatch.setattr(store.checkpoints, "save", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(92)))
    with pytest.raises(SystemExit):
        store.checkpoint("received", {"state": "received"})
    assert (store.root / "runtime/checkpoint-commit.json").exists()
    assert store.manifest()["current_checkpoint"] is None
    assert not (store.root / "runtime/checkpoint-commit.json").exists()
    assert not [event for event in store.history() if event["type"] == "step_succeeded"]

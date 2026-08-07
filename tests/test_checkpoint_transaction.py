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

import json
import threading
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


def test_reader_cannot_roll_back_live_writer_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    writer = ProjectStore(tmp_path, "concurrent"); writer.create()
    reader = ProjectStore(tmp_path, "concurrent")
    save_started = threading.Event()
    release_save = threading.Event()
    original_save = writer.checkpoints.save

    def paused_save(*args, **kwargs):
        save_started.set()
        assert release_save.wait(5)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(writer.checkpoints, "save", paused_save)
    written: list[str] = []
    observed: list[dict] = []
    writer_thread = threading.Thread(target=lambda: written.append(writer.checkpoint("received", {"value": 1})))
    reader_thread = threading.Thread(target=lambda: observed.append(reader.manifest()))
    writer_thread.start(); assert save_started.wait(5)
    reader_thread.start()
    assert reader_thread.is_alive(), "reader must wait for the active checkpoint transaction"
    release_save.set()
    writer_thread.join(5); reader_thread.join(5)

    assert written == ["checkpoints/main/000001-received.json"]
    assert observed[0]["current_checkpoint"]["path"] == written[0]
    assert reader.manifest()["current_checkpoint"]["path"] == written[0]
    assert len([event for event in reader.history() if event["type"] == "step_succeeded"]) == 1

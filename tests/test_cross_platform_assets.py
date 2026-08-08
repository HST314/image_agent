from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from storage import file_lock
from storage.project_store import ProjectStore


def test_runtime_sources_do_not_import_posix_only_fcntl() -> None:
    roots = (Path("agent_core"), Path("configs"), Path("storage"))
    offenders = [
        str(path)
        for root in roots
        for path in root.rglob("*.py")
        if "import fcntl" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_portalocker_facade_accepts_existing_descriptors(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
    try:
        file_lock.lock(descriptor, file_lock.LOCK_EX)
        file_lock.unlock(descriptor)
    finally:
        os.close(descriptor)


def test_project_create_locks_a_binary_stream_and_writes_first_event(tmp_path: Path, monkeypatch) -> None:
    locked_streams = []
    original_lock = file_lock.lock

    def recording_lock(target, flags):
        if getattr(target, "name", "").endswith("events.jsonl"):
            locked_streams.append(target)
            assert not target.closed
            assert target.readable() and target.writable()
        return original_lock(target, flags)

    monkeypatch.setattr(file_lock, "lock", recording_lock)
    store = ProjectStore(tmp_path, "production-event-mode")
    store.create()

    events = store.events.read_all()
    assert len(locked_streams) == 1
    assert [(event["sequence"], event["type"]) for event in events] == [(1, "project_created")]
    assert (store.root / "events/events.jsonl").stat().st_size > 0


def test_project_create_lock_failure_preserves_error_and_removes_partial_project(tmp_path: Path, monkeypatch) -> None:
    unlock_called = False

    def deny_lock(target, flags):
        raise PermissionError(13, "Permission denied")

    def recording_unlock(target):
        nonlocal unlock_called
        unlock_called = True

    monkeypatch.setattr(file_lock, "lock", deny_lock)
    monkeypatch.setattr(file_lock, "unlock", recording_unlock)
    store = ProjectStore(tmp_path, "failed-create")

    with pytest.raises(PermissionError, match="Permission denied"):
        store.create()

    assert unlock_called is False
    assert not store.root.exists()


def test_all_declared_reference_hashes_match_byte_exact_assets() -> None:
    cards = Path("skills/style_cards")
    index = json.loads((cards / "index.json").read_text(encoding="utf-8"))
    for item in index["items"]:
        card = json.loads((cards / item["path"]).read_text(encoding="utf-8"))
        reference = cards / card["reference_image"]["path"]
        assert hashlib.sha256(reference.read_bytes()).hexdigest() == card["reference_image"]["sha256"]
    assert "skills/style_cards/references/** -text" in Path(".gitattributes").read_text(encoding="utf-8")

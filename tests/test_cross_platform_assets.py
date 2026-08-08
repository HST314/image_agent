from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

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


def test_project_create_uses_lock_compatible_event_descriptor(tmp_path: Path, monkeypatch) -> None:
    modes: list[int] = []
    original_open = os.open

    def recording_open(path, flags, mode=0o777):
        if Path(path).name == "events.jsonl":
            modes.append(flags)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", recording_open)
    store = ProjectStore(tmp_path, "production-event-mode")
    store.create()

    assert modes and modes[0] & os.O_RDWR
    assert not modes[0] & os.O_WRONLY
    assert [event["type"] for event in store.events.read_all()] == ["project_created"]


def test_all_declared_reference_hashes_match_byte_exact_assets() -> None:
    cards = Path("skills/style_cards")
    index = json.loads((cards / "index.json").read_text(encoding="utf-8"))
    for item in index["items"]:
        card = json.loads((cards / item["path"]).read_text(encoding="utf-8"))
        reference = cards / card["reference_image"]["path"]
        assert hashlib.sha256(reference.read_bytes()).hexdigest() == card["reference_image"]["sha256"]
    assert "skills/style_cards/references/** -text" in Path(".gitattributes").read_text(encoding="utf-8")

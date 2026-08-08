import json
from pathlib import Path

import pytest

from storage.project_store import LegacyCheckpointReadOnlyError, ProjectStore, atomic_json, content_hash


def _legacy_checkpoint(store: ProjectStore, state: str, data: dict) -> str:
    envelope = {"format_version": 1, "branch": "main", "sequence": 1, "state": state, "data": data}
    envelope["checksum"] = content_hash(envelope)
    relative = f"checkpoints/main/000001-{state}.json"
    atomic_json(store.root / relative, envelope)
    manifest = json.loads((store.root / "manifest.json").read_text())
    manifest["current_checkpoint"] = {"path": relative, "checksum": envelope["checksum"], "branch": "main", "sequence": 1, "state": state}
    atomic_json(store.root / "manifest.json", manifest)
    return relative


def test_new_envelope_preserves_legacy_state_and_adds_cursor(tmp_path: Path):
    store = ProjectStore(tmp_path, "new-envelope"); store.create()
    relative = store.checkpoint("confirmation_build", {"state": "confirmation_build", "phase": "waiting_task_spec_confirmation"})
    raw = json.loads((store.root / relative).read_text())
    assert raw["checkpoint_envelope_version"] == 2
    assert raw["state"] == "confirmation_build"
    assert raw["execution_cursor"]["product_state"] == "waiting_task_spec_confirmation"
    assert raw["compatibility_projection"] == {"state": "confirmation_build", "phase": "waiting_task_spec_confirmation"}


def test_mappable_legacy_is_projected_without_rewrite(tmp_path: Path):
    store = ProjectStore(tmp_path, "legacy-map"); store.create()
    relative = _legacy_checkpoint(store, "confirmation_build", {"state": "confirmation_build", "phase": "waiting_task_spec_confirmation"})
    before = (store.root / relative).read_bytes()
    assert store.resume()["phase"] == "waiting_task_spec_confirmation"
    loaded = store.checkpoints.load(relative)
    assert loaded["checkpoint_envelope_version"] == 1
    assert loaded["execution_cursor"]["product_state"] == "waiting_task_spec_confirmation"
    assert (store.root / relative).read_bytes() == before


def test_unmappable_legacy_is_read_only_but_auditable(tmp_path: Path):
    store = ProjectStore(tmp_path, "legacy-readonly"); store.create()
    relative = _legacy_checkpoint(store, "removed_stage", {"state": "removed_stage"})
    assert store.checkpoints.load(relative)["legacy_read_only"] is True
    with pytest.raises(LegacyCheckpointReadOnlyError, match="仅允许 inspect/history"):
        store.resume()
    with pytest.raises(LegacyCheckpointReadOnlyError, match="禁止从只读记录"):
        store.branch_from(relative)

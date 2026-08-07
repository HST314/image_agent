from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from configs.runtime_policy import RuntimePolicy
from storage.project_store import EventStore, ProjectLockError, ProjectStore


def _hold_lock(root: str, ready, release) -> None:
    store = ProjectStore(root, "p")
    with store.lock():
        ready.set()
        release.wait(10)


def test_runtime_policy_rejects_unknown_invalid_and_hashes(tmp_path: Path):
    raw = yaml.safe_load(Path("configs/runtime.yaml").read_text())
    raw["unknown_behavior"] = True
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        RuntimePolicy.from_file(path)
    raw.pop("unknown_behavior")
    raw["self_check"]["fixed_rounds"] = 10
    raw["self_check"]["max_rounds"] = 2
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="fixed_rounds"):
        RuntimePolicy.from_file(path)


def test_runtime_mode_is_immutable(tmp_path: Path):
    policy = RuntimePolicy.from_file(Path("configs/runtime.yaml"))
    store = ProjectStore(tmp_path, "p")
    store.create({"runtime_policy": policy.snapshot("offline")})
    store.assert_runtime_mode("offline")
    with pytest.raises(ValueError, match="不能切换"):
        store.assert_runtime_mode("real")


def test_five_thread_event_append_has_no_loss_or_corruption(tmp_path: Path):
    events = EventStore(tmp_path / "events.jsonl")
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda i: events.append("parallel", index=i), range(500)))
    records = events.read_all()
    assert len(records) == 500
    assert len({record["event_id"] for record in records}) == 500
    assert {record["index"] for record in records} == set(range(500))


def test_process_lock_competes_and_recovers_after_termination(tmp_path: Path):
    ProjectStore(tmp_path, "p").create()
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    process.start()
    assert ready.wait(10)
    with pytest.raises(ProjectLockError):
        with ProjectStore(tmp_path, "p").lock():
            pass
    process.terminate()
    process.join(10)
    with ProjectStore(tmp_path, "p").lock():
        info = json.loads((tmp_path / "p" / ".lock").read_text())
        assert info["pid"] == os.getpid()

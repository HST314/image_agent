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


def _try_lock(root: str, results) -> None:
    try:
        with ProjectStore(root, "p").lock():
            results.put(("acquired", os.getpid()))
    except ProjectLockError:
        results.put(("blocked", os.getpid()))


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
    holder = context.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    holder.start()
    assert ready.wait(10)

    competing_results = context.Queue()
    competitor = context.Process(target=_try_lock, args=(str(tmp_path), competing_results))
    competitor.start()
    competitor.join(10)
    assert competitor.exitcode == 0
    outcome, competitor_pid = competing_results.get(timeout=2)
    assert outcome == "blocked"
    assert competitor_pid != holder.pid

    holder.terminate()
    holder.join(10)
    assert not holder.is_alive()

    recovery_results = context.Queue()
    recovery = context.Process(target=_try_lock, args=(str(tmp_path), recovery_results))
    recovery.start()
    recovery.join(10)
    assert recovery.exitcode == 0
    outcome, recovery_pid = recovery_results.get(timeout=2)
    assert outcome == "acquired"
    assert recovery_pid != holder.pid

    info = json.loads((tmp_path / "p" / ".lock").read_text())
    assert info["pid"] == recovery_pid


def test_previously_unwired_policy_fields_are_enforced(tmp_path: Path) -> None:
    raw = Path("configs/runtime.yaml").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="approval_required"):
        RuntimePolicy.from_file(_write_policy(tmp_path, raw.replace("approval_required: true", "approval_required: false")))
    with pytest.raises(ValueError, match="image_api_base_url"):
        RuntimePolicy.from_file(_write_policy(tmp_path, raw.replace('image_api_base_url: ""', 'image_api_base_url: "not-a-url"')))


def _write_policy(tmp_path: Path, value: str) -> Path:
    path = tmp_path / "runtime.yaml"
    path.write_text(value, encoding="utf-8")
    return path

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import CalibrationLoop, ManualAction
from agent_core.workflow import SelfCheckPolicy
from storage.assets import normalize_image_asset
from storage.project_store import ProjectStore
from storage.project_store import ProjectLockError


def _asset(uri: str = "https://images.example/base.png"):
    return normalize_image_asset({"uri": uri, "provider": "ark", "model": "seedream"})


def _limited(store: ProjectStore, calls: dict[str, int]):
    def inspect(*_):
        calls["inspect"] += 1
        return {"passed": False, "decision": "continue", "deviations": ["标题对比度不足"],
                "rework_prompt_delta": "增强标题对比度", "confidence": .8}

    def rework(_):
        calls["rework"] += 1
        return _asset(f"https://images.example/rework-{calls['rework']}.png")

    return CalibrationLoop(store, SelfCheckPolicy("solo", "auto", max_rounds=2),
                           inspector=inspect, reworker=rework).run(
        current_asset=_asset(), stable_specification="已确认任务书", constraints=[])


def test_before_limit_reworks_but_limit_waits_without_extra_paid_call(tmp_path: Path):
    store = ProjectStore(tmp_path, "limit"); store.create()
    calls = {"inspect": 0, "rework": 0}
    result = _limited(store, calls)
    assert calls == {"inspect": 2, "rework": 1}
    assert result["phase"] == "waiting_quality_disposition"
    assert result["calibration_status"] == "waiting_human_disposition"
    assert result["failed_items"] == ["标题对比度不足"]
    assert result["waiting"] and not result["termination_satisfied"]
    assert not any(e["type"] == "step_failed" for e in store.history())


def test_limit_cannot_be_bypassed_by_accepting_failed_asset(tmp_path: Path):
    store = ProjectStore(tmp_path, "bypass"); store.create()
    calls = {"inspect": 0, "rework": 0}
    limited = _limited(store, calls)
    with pytest.raises(ValueError, match="未通过图不能"):
        CalibrationLoop(store, SelfCheckPolicy("solo", "auto", max_rounds=2),
                        inspector=lambda *_: pytest.fail("must reuse"), reworker=lambda _: pytest.fail("must not pay")).run(
            current_asset=limited["asset"], stable_specification="已确认任务书", constraints=[],
            approve=lambda _: ManualAction(action="accept_current"), start_round=2)


def _runner_state():
    asset = _asset()
    return {"state": "self_check_iteration", "phase": "waiting_quality_disposition", "waiting": True,
            "asset": asset, "current_asset": asset, "round": 2, "quality_cycle": 1,
            "failed_items": ["标题对比度不足"],
            "inspection": {"passed": False, "decision": "continue", "deviations": ["标题对比度不足"],
                           "rework_prompt_delta": "增强标题对比度", "confidence": .8},
            "calibration_status": "waiting_human_disposition", "termination_satisfied": False,
            "termination_reason": "solo_round_limit", "latest_checked_asset_hash": asset["sha256"],
            "selected_policy": {"termination": "solo", "release": "auto", "max_rounds": 2},
            "task_specification": {"task_id": "t", "version": 1, "facts": [], "parent_hash": None, "content_hash": "s"}}


@pytest.mark.parametrize("action,phase", [("manual_rework", "waiting_human_rework"),
                                           ("abandon", "abandoned")])
def test_non_paid_dispositions_are_legal_idempotent_and_persisted(tmp_path: Path, action: str, phase: str):
    store = ProjectStore(tmp_path, action); store.create(); store.checkpoint("self_check_iteration", _runner_state())
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    options = RunnerOptions(quality_action=action, idempotency_key=f"decision-{action}", actor="operator")
    first = runner.run(store.resume(), options, only_state="self_check_iteration")
    second = runner.run(store.resume(), options, only_state="self_check_iteration")
    assert first["phase"] == second["phase"] == phase
    assert sum(e["type"] == "quality_disposition_recorded" for e in store.history()) == 1
    if action == "abandon":
        assert first["terminal"] and not first["completed"]


def test_continue_generation_opens_fresh_budget_and_repeated_submit_does_not_repay(tmp_path: Path):
    store = ProjectStore(tmp_path, "continue"); store.create(); store.checkpoint("self_check_iteration", _runner_state())
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    paid = []
    runner._image_call = lambda *args, **kwargs: (paid.append((args, kwargs)), {"uri": "https://images.example/new-cycle.png", "provider": "ark", "model": "m"})[1]
    runner._inspect = lambda *_: {"passed": True, "decision": "pass", "deviations": [], "rework_prompt_delta": "", "confidence": .99}
    options = RunnerOptions(quality_action="continue_generation", idempotency_key="continue-cycle-001", actor="operator", expense_confirmed=True)
    first = runner.run(store.resume(), options, only_state="self_check_iteration")
    second = runner.run(store.resume(), options, only_state="self_check_iteration")
    assert first["quality_cycle"] == 2 and first["round"] == 1
    assert first["termination_satisfied"] and len(paid) == 1
    assert second["latest_checked_asset_hash"] == first["latest_checked_asset_hash"] and len(paid) == 1


def test_concurrent_duplicate_disposition_is_serialized_and_paid_once(tmp_path: Path):
    store = ProjectStore(tmp_path, "concurrent"); store.create(); state = _runner_state()
    store.checkpoint("self_check_iteration", state)
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    paid = []
    runner._image_call = lambda *args, **kwargs: (paid.append(kwargs["idempotency_key"]),
        {"uri": "https://images.example/concurrent.png", "provider": "ark", "model": "m"})[1]
    runner._inspect = lambda *_: {"passed": True, "decision": "pass", "deviations": [],
                                  "rework_prompt_delta": "", "confidence": .99}
    options = RunnerOptions(quality_action="continue_generation", idempotency_key="concurrent-key-001", actor="operator", expense_confirmed=True)
    def submit(_):
        try:
            return runner.run(state, options, only_state="self_check_iteration")
        except ProjectLockError:
            return "locked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    successful = next(result for result in results if result != "locked")
    retried = runner.run(store.resume(), options, only_state="self_check_iteration")
    assert len(paid) == 1
    assert retried["latest_checked_asset_hash"] == successful["latest_checked_asset_hash"]
    assert sum(e["type"] == "quality_disposition_recorded" for e in store.history()) == 1


def test_disposition_requires_limit_gate_actor_and_idempotency_key(tmp_path: Path):
    store = ProjectStore(tmp_path, "gate"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    for state, options in [
        ({**_runner_state(), "phase": "round_checkpointed"}, RunnerOptions(quality_action="abandon", idempotency_key="gate-key-1", actor="op")),
        (_runner_state(), RunnerOptions(quality_action="abandon", actor="op")),
        (_runner_state(), RunnerOptions(quality_action="abandon", idempotency_key="gate-key-2")),
    ]:
        with pytest.raises(ValueError):
            runner.run(state, options, only_state="self_check_iteration")


def test_continue_generation_requires_explicit_expense_confirmation(tmp_path: Path):
    store = ProjectStore(tmp_path, "expense-gate"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    paid = []
    runner._image_call = lambda *args, **kwargs: paid.append(1)
    with pytest.raises(ValueError, match="费用"):
        runner.run(_runner_state(), RunnerOptions(quality_action="continue_generation",
            idempotency_key="expense-1", actor="operator"), only_state="self_check_iteration")
    assert paid == []


def test_fixed_policy_stops_immediately_on_first_pass(tmp_path: Path):
    store = ProjectStore(tmp_path, "fixed-pass"); store.create(); calls = []
    result = CalibrationLoop(store, SelfCheckPolicy("fix", "auto", fixed_rounds=3, max_rounds=3,
        stop_early_on_pass=False), inspector=lambda *_: calls.append(1) or {
            "passed": True, "decision": "pass", "deviations": [], "rework_prompt_delta": "", "confidence": .9},
        reworker=lambda _: pytest.fail("must not rework")).run(current_asset=_asset(),
        stable_specification="spec", constraints=[])
    assert result["round"] == 1 and len(calls) == 1


def test_human_rework_paid_call_is_idempotent(tmp_path: Path):
    store = ProjectStore(tmp_path, "human-idem"); store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    paid = []
    runner._image_call = lambda *args, **kwargs: paid.append(kwargs["idempotency_key"]) or _asset("https://images.example/human.png")
    state = {**_runner_state(), "state": "human_prompt_iteration", "phase": "waiting_human_rework"}
    options = RunnerOptions(human_prompt="提高对比度", idempotency_key="human-1", actor="op")
    first = runner.run(state, options, only_state="human_prompt_iteration")
    second = runner.run(state, options, only_state="human_prompt_iteration")
    assert first["asset"]["sha256"] == second["asset"]["sha256"] and len(paid) == 1

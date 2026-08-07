from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from agent_core.guided_edit import GuidedEditRequest, compose_guidance
from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from storage.project_store import ProjectStore


def _png(size=(120, 60), color="white") -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, "PNG")
    return stream.getvalue()


def _request(asset_id: str, *, size=(120, 60), branch="main", round=1):
    return {"parent_asset_id": asset_id, "branch": branch,
            "coordinate_space": "source_image_pixels", "source_width": size[0], "source_height": size[1],
            "prompt": "把圈出的区域改为蓝色", "actor": "tester", "round": round,
            "annotations": [
                {"type": "rectangle", "x": 10, "y": 5, "width": 30, "height": 20,
                 "color": "#ff0000", "stroke_width": 3},
                {"type": "brush", "points": [{"x": 70, "y": 10}, {"x": 90, "y": 30}],
                 "color": "#00ff00", "stroke_width": 5},
            ]}


@pytest.mark.parametrize("size", [(160, 90), (90, 160), (400, 40)])
def test_intrinsic_pixel_coordinates_compose_without_aspect_ratio_mapping(size):
    request = GuidedEditRequest.model_validate(_request("artifact_" + "a" * 64, size=size))
    content, media_type, width, height = compose_guidance(_png(size), request)
    image = Image.open(io.BytesIO(content)).convert("RGB")
    assert (width, height, media_type) == (*size, "image/png")
    assert image.getpixel((10, 5))[0] > 200
    assert image.getpixel((80, 20))[1] > 200


@pytest.mark.parametrize("mutation", [
    {"prompt": "   "}, {"annotations": []}, {"coordinate_space": "css_pixels"},
])
def test_empty_or_noncanonical_annotation_inputs_are_rejected(mutation):
    raw = {**_request("artifact_" + "a" * 64), **mutation}
    with pytest.raises(ValidationError):
        GuidedEditRequest.model_validate(raw)


def _runner(tmp_path: Path):
    store = ProjectStore(tmp_path, "project-a")
    store.create({"runtime_policy": RuntimePolicy.from_file(Path("configs/runtime.yaml")).snapshot("offline")})
    source = store.artifacts.save_bytes(_png(), suffix=".png", metadata={
        "media_type": "image/png", "provider": "fixture", "model": "fixture", "mock": False,
        "project_id": "project-a", "branch": "main", "width": 120, "height": 60})
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    state = {"state": "human_prompt_iteration", "phase": "waiting_human_rework",
             "asset": source, "current_asset": source, "latest_checked_asset_hash": source["sha256"],
             "final_confirmation": {"asset_sha256": source["sha256"]}}
    return store, runner, source, state


def test_guided_edit_saves_guidance_chain_invalidates_approval_and_is_idempotent(tmp_path):
    store, runner, source, state = _runner(tmp_path)
    calls = []
    def render(*args, **kwargs):
        calls.append((args, kwargs))
        return store.artifacts.save_bytes(_png(color=("blue" if len(calls) == 1 else "green")), suffix=".png", metadata={
            "media_type": "image/png", "provider": "fixture", "model": "fixture", "mock": False,
            "project_id": "project-a", "branch": "main", "width": 120, "height": 60})
    runner._image_call = render
    options = RunnerOptions(guided_edit=_request(source["artifact_id"]), idempotency_key="stable-edit-001")
    first = runner.run(state, options, only_state="human_prompt_iteration")
    second = runner.run(state, options, only_state="human_prompt_iteration")
    assert len(calls) == 1
    assert first["guided_edit"] == second["guided_edit"]
    assert first["guided_edit"]["parent_asset_id"] == source["artifact_id"]
    assert store.artifacts.resolve(first["guided_edit"]["guidance_asset_id"]).is_file()
    assert first["phase"] == "waiting_reinspection"
    assert first["latest_checked_asset_hash"] is None and first["final_confirmation"] is None
    assert store.artifacts.resolve(source["artifact_id"]).read_bytes() == _png()
    third = runner.run({**first, "phase": "waiting_human_rework"}, RunnerOptions(
        guided_edit=_request(first["asset"]["artifact_id"], round=2), idempotency_key="stable-edit-002"),
        only_state="human_prompt_iteration")
    assert len(calls) == 2
    assert third["guided_edit"]["parent_asset_id"] == first["asset"]["artifact_id"]
    assert source["artifact_id"] != first["asset"]["artifact_id"] != third["asset"]["artifact_id"]


@pytest.mark.parametrize("mutation,message", [
    ({"branch": "other"}, "当前执行分支"),
    ({"parent_asset_id": "artifact_" + "f" * 64}, "最新可编辑资产"),
])
def test_cross_branch_or_nonhead_asset_is_rejected_before_paid_call(tmp_path, mutation, message):
    _store, runner, source, state = _runner(tmp_path)
    calls = []
    runner._image_call = lambda *args, **kwargs: calls.append(1)
    request = {**_request(source["artifact_id"]), **mutation}
    with pytest.raises(ValueError, match=message):
        runner.run(state, RunnerOptions(guided_edit=request, idempotency_key="stable-edit-002"),
                   only_state="human_prompt_iteration")
    assert calls == []


def test_provider_failure_writes_no_completed_edit(tmp_path):
    store, runner, source, state = _runner(tmp_path)
    runner._image_call = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider failed"))
    with pytest.raises(RuntimeError, match="provider failed"):
        runner.run(state, RunnerOptions(guided_edit=_request(source["artifact_id"]),
                   idempotency_key="stable-edit-003"), only_state="human_prompt_iteration")
    assert not any(event["type"] == "human_rework_completed" for event in store.history())


def test_asset_with_foreign_project_ownership_is_rejected(tmp_path):
    store, runner, _source, state = _runner(tmp_path)
    foreign = store.artifacts.save_bytes(_png(), suffix=".png", metadata={
        "media_type": "image/png", "provider": "fixture", "model": "fixture", "mock": False,
        "project_id": "project-b", "branch": "main", "width": 120, "height": 60})
    state = {**state, "asset": foreign, "current_asset": foreign}
    calls = []
    runner._image_call = lambda *args, **kwargs: calls.append(1)
    with pytest.raises(ValueError, match="跨项目"):
        runner.run(state, RunnerOptions(guided_edit=_request(foreign["artifact_id"]),
                   idempotency_key="stable-edit-foreign"), only_state="human_prompt_iteration")
    assert calls == []

import json

import pytest

from agent_core.models import StyleUnderstandingOutput, VisualInspectionOutput
from agent_core.structured_output import RecoverableStructuredOutputError, validate_with_one_repair
from tests.test_p1_04_style_vlm import _confirmed_runner_data


def _style(**changes):
    value = {
        "style_index": "STYLE-001", "style_summary": "摘要", "title": "标题",
        "composition": "构图", "material": "材质", "lighting": "光影",
        "narrative": "叙事", "graphic_language": "图形", "fit_reason": "适配",
        "artistic_philosophy": "理念", "adaptable_mechanism": "机制",
        "prohibited_copy_elements": ["主体"], "major_risk": "风险", "prompt_supplement": "补充",
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("first", ["{broken", {"style_index": "STYLE-001"}, _style(lighting="")])
def test_style_malformed_missing_or_illegal_is_repaired_once(first):
    calls = []
    responses = iter([first, _style()])
    result = validate_with_one_repair(
        output_kind="style_understanding", model=StyleUnderstandingOutput,
        invoke=lambda prompt: calls.append(prompt) or next(responses), prompt="original",
        schema=StyleUnderstandingOutput.model_json_schema(),
    )
    assert result.style_index == "STYLE-001"
    assert len(calls) == 2
    assert "校验错误" in calls[1] and "原响应" in calls[1]


def test_style_bound_identity_is_repaired_instead_of_accepted():
    responses = iter([_style(style_index="STYLE-999"), _style()])
    result = validate_with_one_repair(
        output_kind="style_understanding", model=StyleUnderstandingOutput,
        invoke=lambda _prompt: next(responses), prompt="original",
        schema=StyleUnderstandingOutput.model_json_schema(), expected_values={"style_index": "STYLE-001"},
    )
    assert result.style_index == "STYLE-001"


@pytest.mark.parametrize("wrapped", [
    "```json\n" + json.dumps(_style(), ensure_ascii=False) + "\n```",
    "以下是结果：" + json.dumps(_style(), ensure_ascii=False) + "。结束",
])
def test_json_is_extracted_without_spending_repair(wrapped):
    calls = []
    result = validate_with_one_repair(output_kind="style_understanding", model=StyleUnderstandingOutput,
        invoke=lambda prompt: calls.append(prompt) or wrapped, prompt="original",
        schema=StyleUnderstandingOutput.model_json_schema())
    assert result.style_index == "STYLE-001" and len(calls) == 1


def test_repair_prompt_redacts_secret_and_cannot_change_valid_fields():
    calls = []
    first = _style(lighting="")
    first["major_risk"] = "api_key=TOPSECRET"
    repaired = _style(lighting="修复光影", title="偷偷改标题", major_risk="api_key=TOPSECRET")
    with pytest.raises(RecoverableStructuredOutputError):
        validate_with_one_repair(output_kind="style_understanding", model=StyleUnderstandingOutput,
            invoke=lambda prompt: calls.append(prompt) or (first if len(calls) == 1 else repaired), prompt="original",
            schema=StyleUnderstandingOutput.model_json_schema())
    assert "TOPSECRET" not in calls[1] and '"title"' in calls[1]


def test_second_failure_is_recoverable_and_redacts_sensitive_raw():
    recorded = []
    responses = iter(["api_key=first-secret {", "Authorization: Bearer-second-secret {"])
    with pytest.raises(RecoverableStructuredOutputError) as caught:
        validate_with_one_repair(
            output_kind="style_understanding", model=StyleUnderstandingOutput,
            invoke=lambda _prompt: next(responses), prompt="original",
            schema=StyleUnderstandingOutput.model_json_schema(), on_failure=recorded.append,
        )
    assert caught.value.retryable is True and recorded == [caught.value]
    assert "second-secret" not in caught.value.redacted_output
    assert "[REDACTED]" in caught.value.redacted_output


def test_visual_inspection_never_defaults_to_pass_and_repairs_only_once():
    calls = []
    responses = iter([
        {"deviations": [], "rework_prompt_delta": ""},
        {"passed": False, "decision": "continue", "deviations": ["偏差"], "rework_prompt_delta": "修正", "confidence": 0.7},
    ])
    result = validate_with_one_repair(
        output_kind="visual_inspection", model=VisualInspectionOutput,
        invoke=lambda prompt: calls.append(prompt) or next(responses), prompt="inspect",
        schema=VisualInspectionOutput.model_json_schema(),
    )
    assert result.passed is False and result.decision == "continue" and len(calls) == 2


def test_visual_pass_flag_and_decision_must_agree():
    responses = iter([
        {"passed": True, "decision": "continue", "deviations": [], "rework_prompt_delta": "", "confidence": 0.8},
        {"passed": True, "decision": "pass", "deviations": [], "rework_prompt_delta": "", "confidence": 0.8},
    ])
    result = validate_with_one_repair(
        output_kind="visual_inspection", model=VisualInspectionOutput,
        invoke=lambda _prompt: next(responses), prompt="inspect",
        schema=VisualInspectionOutput.model_json_schema(),
    )
    assert result.decision == "pass"


def test_visual_semantic_contradiction_is_rejected():
    invalid = {"passed": True, "decision": "pass", "deviations": ["仍有偏差"],
               "rework_prompt_delta": "继续改", "confidence": 0.9}
    with pytest.raises(RecoverableStructuredOutputError):
        validate_with_one_repair(output_kind="visual_inspection", model=VisualInspectionOutput,
            invoke=lambda _: invalid, prompt="inspect", schema=VisualInspectionOutput.model_json_schema())


def test_visual_second_failure_does_not_invoke_rework():
    model_calls = 0
    paid_rework_calls = 0

    def invoke(_prompt):
        nonlocal model_calls
        model_calls += 1
        return "not json"

    with pytest.raises(RecoverableStructuredOutputError):
        validate_with_one_repair(
            output_kind="visual_inspection", model=VisualInspectionOutput, invoke=invoke,
            prompt="inspect", schema=VisualInspectionOutput.model_json_schema(),
        )
    assert model_calls == 2
    assert paid_rework_calls == 0


def test_runner_persists_redacted_style_failure_before_any_paid_image_call(tmp_path):
    from pathlib import Path
    from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
    from storage.project_store import ProjectStore

    store = ProjectStore(tmp_path, "p1-06-style-failure")
    store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    runner.offline_mode = False
    counts = {"vlm": 0, "image": 0}

    def invalid(_image, _prompt):
        counts["vlm"] += 1
        return "token=super-secret {broken"

    runner._style_vlm_call = invalid
    runner._image_call = lambda *_args, **_kwargs: counts.__setitem__("image", counts["image"] + 1)
    with pytest.raises(RecoverableStructuredOutputError):
        runner.run(_confirmed_runner_data(), RunnerOptions(), only_state="initial_candidate_generation")

    assert counts == {"vlm": 2, "image": 0}
    event = next(item for item in store.history() if item["type"] == "structured_output_recovery_required")
    assert event["retryable"] is True and "super-secret" not in event["raw_output"]

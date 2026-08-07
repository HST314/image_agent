import base64
import json
from pathlib import Path

import pytest

from agent_core.models import ImageTaskCard, SourceRef, TaskConfirmationDoc
from skills.style_idea_generator import StyleIdeaGenerator
from skills.style_loader import StyleCardLoader


STYLE_INDEX = Path("skills/style_cards/index.json")
RELEVANT_TASK_TEXT = "编排网格 主视觉候选 编辑式动线 上下文预览 极简信号"


def _task() -> ImageTaskCard:
    return ImageTaskCard(
        task_id="p1-04",
        project_id="style-vlm",
        source_refs=[SourceRef(ref_id="source", ref_type="text")],
        deliverable_goal="品牌新品海报",
        usage_context="商场竖屏",
        known_facts={"品牌": "京彩", "合规": "不得使用未经授权标识"},
    )


def _payload(style_index: str) -> dict[str, object]:
    return {
        "style_index": style_index,
        "style_summary": "克制且清晰的视觉秩序",
        "title": "结构化视觉方向",
        "composition": "使用非对称信息层级",
        "material": "哑光纸张与柔和侧光",
        "fit_reason": "适合新品信息分层",
        "artistic_philosophy": "以秩序服务信息表达",
        "adaptable_mechanism": "借鉴节奏、留白与材质关系",
        "prohibited_copy_elements": ["参考图主体", "参考图构图", "参考图文字", "参考图标识", "独特表达"],
        "major_risk": "避免风格压过品牌识别",
        "prompt_supplement": "用抽象节奏和留白建立层级",
    }


def _confirmation() -> TaskConfirmationDoc:
    return TaskConfirmationDoc(
        task_id="p1-04", confirmed_facts=[], default_handling_for_unknowns=[]
    )


class RecordingVLM:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_at = fail_at

    def inspect(self, image_uri: str, prompt: str) -> dict[str, object]:
        self.calls.append((image_uri, prompt))
        if self.fail_at == len(self.calls):
            raise RuntimeError("vlm unavailable")
        style_index = json.loads(prompt.split("绑定身份：", 1)[1].splitlines()[0])["style_index"]
        return _payload(style_index)


def test_five_controlled_images_are_interpreted_once_and_bound_to_style_index():
    cards = StyleCardLoader(STYLE_INDEX).select_distinct(count=5, task_text=RELEVANT_TASK_TEXT)
    client = RecordingVLM()
    ideas = StyleIdeaGenerator(client=client).generate(
        task_card=_task(),
        confirmation_doc=_confirmation(),
        style_cards=cards,
    )

    assert len(client.calls) == len(ideas) == 5
    assert [idea.style_index for idea in ideas] == [card.style_index for card in cards]
    for (image_uri, prompt), card, idea in zip(client.calls, cards, ideas):
        assert image_uri.startswith("data:image/")
        assert base64.b64decode(image_uri.split(",", 1)[1]) == (
            STYLE_INDEX.parent / card.reference_image.path
        ).read_bytes()
        assert json.loads(prompt.split("绑定身份：", 1)[1].splitlines()[0]) == {
            "style_id": card.style_id,
            "style_index": card.style_index,
        }
        assert idea.style_summary and idea.prohibited_copy_elements
        assert all(
            getattr(idea, field)
            for field in (
                "title", "composition", "material", "fit_reason", "artistic_philosophy",
                "adaptable_mechanism", "major_risk", "prompt_supplement",
            )
        )


def test_vlm_identity_mismatch_and_failure_are_explicit():
    cards = StyleCardLoader(STYLE_INDEX).select_distinct(count=5, task_text=RELEVANT_TASK_TEXT)

    class WrongIdentity(RecordingVLM):
        def inspect(self, image_uri: str, prompt: str) -> dict[str, object]:
            payload = super().inspect(image_uri, prompt)
            payload["style_index"] = "STYLE-999"
            return payload

    with pytest.raises(RuntimeError, match="绑定"):
        StyleIdeaGenerator(client=WrongIdentity()).generate(
            task_card=_task(), confirmation_doc=_confirmation(), style_cards=cards
        )

    failing = RecordingVLM(fail_at=3)
    with pytest.raises(RuntimeError, match="VLM"):
        StyleIdeaGenerator(client=failing).generate(
            task_card=_task(), confirmation_doc=_confirmation(), style_cards=cards
        )
    assert len(failing.calls) == 3


def test_candidate_prompts_use_style_text_only_and_identical_hard_constraints():
    from agent_core.workflow_runner import build_style_candidate_prompt

    cards = StyleCardLoader(STYLE_INDEX).select_distinct(count=5, task_text=RELEVANT_TASK_TEXT)
    ideas = StyleIdeaGenerator(offline_mode=True).generate(
        task_card=_task(), confirmation_doc=_confirmation(), style_cards=cards
    )
    hard_constraints = {
        "content": ["新品名称必须清晰"],
        "brand": ["品牌色为红色"],
        "space": ["1080x1920 竖版"],
        "compliance": ["不得使用未经授权标识"],
    }
    prompts = [build_style_candidate_prompt(idea, hard_constraints, "户外广告") for idea in ideas]
    hard_block = json.dumps(hard_constraints, ensure_ascii=False, sort_keys=True)

    assert len(set(prompts)) == 5
    assert all(prompt.count(hard_block) == 1 for prompt in prompts)
    assert all("只借鉴抽象视觉语言" in prompt and "不得复制参考图" in prompt for prompt in prompts)
    forbidden = [card.reference_image.path for card in cards]
    forbidden += [str((STYLE_INDEX.parent / card.reference_image.path).resolve()) for card in cards]
    assert all(not any(value in prompt for value in forbidden) for prompt in prompts)


def test_unconfirmed_task_blocks_vlm_and_image_calls(tmp_path: Path):
    from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
    from storage.project_store import ProjectStore

    store = ProjectStore(tmp_path, "p1-04-gate")
    store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    counts = {"vlm": 0, "image": 0}
    runner._style_vlm_call = lambda *_args, **_kwargs: counts.__setitem__("vlm", counts["vlm"] + 1)
    runner._image_call = lambda *_args, **_kwargs: counts.__setitem__("image", counts["image"] + 1)
    data = {
        "task_card": _task().model_dump(mode="json"),
        "task_specification": {
            "task_id": "p1-04", "version": 1, "facts": [], "parent_hash": None, "content_hash": "hash",
        },
    }
    with pytest.raises(ValueError, match="尚未人工确认"):
        runner.run(data, RunnerOptions(), only_state="initial_candidate_generation")
    assert counts == {"vlm": 0, "image": 0}


def _confirmed_runner_data() -> dict[str, object]:
    return {
        "task_card": _task().model_dump(mode="json"),
        "task_specification": {
            "task_id": "p1-04",
            "version": 1,
            "facts": [
                {"label": "品牌", "value": "京彩", "provenance": "task", "status": "confirmed"},
                {"label": "空间尺寸", "value": "1080x1920", "provenance": "task", "status": "confirmed"},
                {"label": "合规要求", "value": "不得使用未经授权标识", "provenance": "task", "status": "confirmed"},
            ],
            "parent_hash": None,
            "content_hash": "confirmed-hash",
        },
        "task_spec_confirmation": {
            "task_spec_version": 1,
            "subject_sha256": "confirmed-hash",
            "actor": "reviewer",
            "confirmed_at": "2026-08-07T00:00:00Z",
        },
    }


def test_runner_makes_five_vlm_calls_then_five_text_only_image_calls(tmp_path: Path):
    from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
    from storage.project_store import ProjectStore

    store = ProjectStore(tmp_path, "p1-04-five")
    store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    runner.offline_mode = False
    vlm_calls: list[tuple[str, str]] = []
    image_calls: list[tuple[str, list[str], int | None]] = []

    def vlm(image_uri: str, prompt: str) -> dict[str, object]:
        vlm_calls.append((image_uri, prompt))
        style_index = json.loads(prompt.split("绑定身份：", 1)[1].splitlines()[0])["style_index"]
        return _payload(style_index)

    def image(_state: str, prompt: str, references: list[str], *, index: int | None = None):
        image_calls.append((prompt, references, index))
        return {"uri": f"mock://candidate/{index}", "sha256": str(index), "mock": True}

    runner._style_vlm_call = vlm
    runner._image_call = image
    result = runner.run(
        _confirmed_runner_data(), RunnerOptions(), only_state="initial_candidate_generation"
    )

    assert len(vlm_calls) == len(image_calls) == len(result["candidates"]) == 5
    assert all(references == [] for _, references, _ in image_calls)
    assert {index for _, _, index in image_calls} == set(range(5))
    hard_blocks = [prompt.split("【内容/品牌/空间/合规硬约束（不得改写）】\n", 1)[1].split("\n【品类硬约束】", 1)[0] for prompt, _, _ in image_calls]
    assert len(set(hard_blocks)) == 1
    serialized_calls = json.dumps(image_calls, ensure_ascii=False)
    assert "data:image/" not in serialized_calls and "references/" not in serialized_calls


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
def test_any_vlm_failure_blocks_all_image_calls(tmp_path: Path, fail_at: int):
    from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
    from storage.project_store import ProjectStore

    store = ProjectStore(tmp_path, f"p1-04-vlm-fail-{fail_at}")
    store.create()
    runner = WorkflowRunner(store, Path("configs/model_config.yaml"), offline_mode=True)
    runner.offline_mode = False
    counts = {"vlm": 0, "image": 0}

    def vlm(_image_uri: str, prompt: str) -> dict[str, object]:
        counts["vlm"] += 1
        if counts["vlm"] == fail_at:
            raise RuntimeError("provider unavailable")
        style_index = json.loads(prompt.split("绑定身份：", 1)[1].splitlines()[0])["style_index"]
        return _payload(style_index)

    runner._style_vlm_call = vlm
    runner._image_call = lambda *_args, **_kwargs: counts.__setitem__("image", counts["image"] + 1)
    with pytest.raises(RuntimeError, match="VLM"):
        runner.run(_confirmed_runner_data(), RunnerOptions(), only_state="initial_candidate_generation")
    assert counts == {"vlm": fail_at, "image": 0}

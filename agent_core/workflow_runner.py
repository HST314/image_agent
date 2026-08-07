"""Production workflow runner and explicit state-handler registry."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_core.batch import CandidateBatchGenerator
from agent_core.models import ImageTaskCard, ModelRole, TaskSpecification, VisualCheckResult
from agent_core.state_machine import RecoverableWorkflow
from agent_core.workflow import TRANSITIONS, SelfCheckPolicy, validate_transition
from calibrator.calibration_loop import CalibrationLoop, ManualAction
from interaction.confirmation_builder import specification_from_task, specification_to_markdown, update_specification_from_markdown
from interaction.question_generator import generate_question_card
from model_router.clients import build_text_client, build_vlm_client
from model_router.gateway import RuntimeModelGateway
from model_router.router import ModelRoute, ModelRouter
from render_clients.ark_client import ArkImageRenderClient
from render_clients.payload_mapper import build_render_payload
from storage.project_store import ProjectStore, content_hash
from storage.assets import normalize_image_asset, persist_image_asset
from interaction.presenter import Presenter
from configs.runtime_policy import RuntimePolicy
from model_router.executor import ModelExecutor


class SkillLoadError(RuntimeError):
    """A required external skill could not be loaded safely."""

Handler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class RunnerOptions:
    selected_id: str | None = None
    manual_action: ManualAction | None = None
    human_prompt: str | None = None
    edited_markdown: str | None = None
    task_spec_action: str | None = None
    final_action: str | None = None
    actor: str | None = None
    clarification_answers: dict[str, Any] | None = None


class WorkflowRunner:
    """Run registered real handlers and checkpoint every successful boundary."""

    ORDER = ("intake_clarify", "confirmation_build", "initial_candidate_generation",
             "master_candidate_selection", "self_check_iteration", "human_prompt_iteration", "final_approval")

    def __init__(self, store: ProjectStore, config: Path, *, offline_mode: bool = False,
                 runtime_policy: RuntimePolicy | None = None,
                 output: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.policy = runtime_policy or RuntimePolicy.from_file(Path("configs/runtime.yaml"))
        self.store.assert_runtime_mode("offline" if offline_mode else "real")
        executor = ModelExecutor(max_attempts=self.policy.max_render_retries + 1, timeout=self.policy.model_timeout_seconds)
        self.gateway = RuntimeModelGateway(store, ModelRouter.from_file(config), executor=executor, offline_mode=offline_mode)
        self.offline_mode = offline_mode
        self.output = output or (lambda _: None)
        self.presenter = Presenter()
        self.workflow = RecoverableWorkflow(store)
        self.handlers: dict[str, Handler] = {
            "intake_clarify": self._clarify, "confirmation_build": self._confirmation,
            "initial_candidate_generation": self._candidates, "master_candidate_selection": self._selection,
            "self_check_iteration": self._self_check, "human_prompt_iteration": self._human_rework,
            "final_approval": self._final,
        }

    def next_state(self, snapshot: dict[str, Any] | None) -> str:
        if snapshot is None or not snapshot.get("state"): return self.ORDER[0]
        if snapshot.get("phase") in {"waiting_task_spec_confirmation", "waiting_final_confirmation", "waiting_clarification", "waiting_master_selection"}:
            return str(snapshot.get("state"))
        if snapshot.get("phase") == "waiting_human_rework":
            return "human_prompt_iteration"
        if snapshot.get("phase") == "waiting_reinspection":
            return "self_check_iteration"
        current = str(snapshot.get("state", ""))
        if current not in self.ORDER: raise ValueError("检查点中的流程状态无法识别。")
        index = self.ORDER.index(current)
        if index + 1 >= len(self.ORDER): raise ValueError("工程已经完成最终确认。")
        return self.ORDER[index + 1]

    def run(self, snapshot: dict[str, Any] | None, options: RunnerOptions, *, only_state: str | None = None) -> dict[str, Any]:
        with self.store.lock():
            return self._run_locked(snapshot, options, only_state=only_state)

    def _run_locked(self, snapshot: dict[str, Any] | None, options: RunnerOptions, *, only_state: str | None = None) -> dict[str, Any]:
        data = dict(snapshot or {}); target = only_state or self.next_state(snapshot)
        while True:
            current = str(data.get("state", ""))
            if current and current != target:
                validate_transition(current, target)
            handler = self.handlers[target]
            self.store.start_step(target, input_hash=content_hash(data))
            try:
                result = handler(data, options.__dict__)
                data = {**data, **result, "state": target}
                # Waiting is a successful recoverable boundary, not a failed state.
                self.store.checkpoint(target, data)
            except Exception as exc:
                self.store.fail_step(target, {"code": type(exc).__name__, "message": str(exc), "retryable": True})
                raise
            if only_state or data.get("waiting") or target == "final_approval": return data
            target = self.next_state(data)

    def _clarify(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        task = ImageTaskCard.model_validate(data["task_card"])
        fingerprints = set(data.get("previous_fingerprints", []))
        already_asked = int(data.get("clarification_asked_count", 0))
        rounds = int(data.get("clarification_round_count", 0))
        if data.get("phase") == "waiting_clarification":
            answers = options.get("clarification_answers")
            if not answers: return {"waiting": True, "phase": "waiting_clarification"}
            facts = dict(task.known_facts); facts.update(answers)
            task = task.model_copy(update={"known_facts": facts, "unknowns": {k:v for k,v in task.unknowns.items() if k not in answers}})
            return {"task_card": task.model_dump(mode="json"), "clarification_answers": answers, "waiting": False, "phase": "clarification_completed",
                    "previous_fingerprints": sorted(fingerprints), "clarification_asked_count": already_asked,
                    "clarification_round_count": rounds,
                    "clarification_remaining_budget": max(0, self.policy.clarification_total_budget - already_asked)}
        if rounds >= self.policy.max_clarify_rounds:
            return {"waiting": False, "phase": "clarification_round_limit_reached",
                    "previous_fingerprints": sorted(fingerprints), "clarification_asked_count": already_asked,
                    "clarification_round_count": rounds,
                    "clarification_remaining_budget": max(0, self.policy.clarification_total_budget - already_asked)}
        if self.offline_mode:
            card = generate_question_card(task, previous_fingerprints=fingerprints, already_asked=already_asked)
        else:
            card = self.gateway.call("intake_clarify", ModelRole.REASONING_LLM,
                lambda route: generate_question_card(task, self._text(route), question_count=self.policy.default_question_count,
                    mode=self.policy.question_mode, previous_fingerprints=fingerprints, already_asked=already_asked,
                    max_auto_questions=self.policy.max_auto_questions, total_budget=self.policy.clarification_total_budget,
                    stream_handler=self.output if self.policy.stream_model_output else None,
                    error_recorder=lambda e: self.store.events.append(**{"event_type": "model_parse_failed", **e})),
                messages=[{"role":"user","content":"分析任务中真正阻塞的未知项"}], variables={"task":task.model_dump(mode="json")},
                template_id="clarification", template_version="2", input_refs=[r.ref_id for r in task.source_refs])
        if card.questions:
            fingerprints.update(q.semantic_fingerprint for q in card.questions)
            asked = already_asked + len(card.questions)
            return {"question_card": card.model_dump(mode="json"), "waiting": True, "phase": "waiting_clarification",
                    "previous_fingerprints": sorted(fingerprints), "clarification_asked_count": asked,
                    "clarification_round_count": rounds + 1,
                    "clarification_remaining_budget": max(0, self.policy.clarification_total_budget - asked)}
        return {"question_card": card.model_dump(mode="json"), "waiting": False,
                "previous_fingerprints": sorted(fingerprints), "clarification_asked_count": already_asked,
                "clarification_round_count": rounds,
                "clarification_remaining_budget": max(0, self.policy.clarification_total_budget - already_asked)}

    def _confirmation(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        spec = TaskSpecification.model_validate(data["task_specification"]) if data.get("task_specification") else specification_from_task(ImageTaskCard.model_validate(data["task_card"]))
        if options.get("edited_markdown"):
            spec = update_specification_from_markdown(spec, options["edited_markdown"])
            self.store.events.append("task_spec_confirmation_invalidated", reason="task_spec_edited",
                                     task_spec_version=spec.version, subject_sha256=spec.content_hash)
        confirmation = data.get("task_spec_confirmation")
        if options.get("task_spec_action") == "confirm":
            actor = str(options.get("actor") or "").strip()
            if not actor:
                raise ValueError("确认任务书必须提供 actor。")
            confirmation = {"task_spec_version": spec.version, "subject_sha256": spec.content_hash,
                            "actor": actor, "confirmed_at": datetime.now(timezone.utc).isoformat()}
            self.store.events.append("task_spec_confirmed", **confirmation)
        valid = bool(confirmation and confirmation.get("task_spec_version") == spec.version
                     and confirmation.get("subject_sha256") == spec.content_hash)
        return {"task_specification": spec.model_dump(mode="json"), "task_markdown": specification_to_markdown(spec),
                "task_spec_confirmation": confirmation if valid else None, "waiting": not valid,
                "phase": "task_spec_confirmed" if valid else "waiting_task_spec_confirmation"}

    def _candidates(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        spec = TaskSpecification.model_validate(data["task_specification"])
        confirmation = data.get("task_spec_confirmation") or {}
        if (confirmation.get("task_spec_version") != spec.version
                or confirmation.get("subject_sha256") != spec.content_hash
                or not confirmation.get("actor") or not confirmation.get("confirmed_at")):
            raise ValueError("当前任务书版本尚未人工确认，禁止调用付费生图。")
        task_card = ImageTaskCard.model_validate(data["task_card"])
        
        # 1. 匹配广告品类 Skill
        category_skill = None
        degraded_reasons: list[dict[str, str]] = []
        try:
            from skills.category_library_adapter import CategoryLibraryAdapter
            lib_path = Path(__file__).parent.parent / "skills/category_libraries/advertising_category_library_v2.json"
            if not lib_path.is_file():
                raise FileNotFoundError(f"品类 Skill 库不存在：{lib_path.name}")
            adapter = CategoryLibraryAdapter(lib_path)
            match = adapter.load_for_task(task_card)
            if not match:
                raise LookupError(f"品类 {task_card.category_ref.category_id} 无匹配 Skill")
            category_skill = match.skill
        except Exception as exc:
            self._handle_skill_failure("category_library", exc, degraded_reasons)

        # 2. 从艺术风格库筛选 5 种不同机制的方向，并生成文本卡片
        from skills.style_loader import StyleCardLoader
        from skills.style_idea_generator import StyleIdeaGenerator
        
        style_cards = []
        try:
            style_path = Path(__file__).parent.parent / "skills/style_cards/index.json"
            if style_path.exists():
                task_text = " ".join([task_card.deliverable_goal, task_card.usage_context, *map(str, task_card.known_facts.values())])
                style_cards = StyleCardLoader(style_path).select_distinct(count=self.policy.candidate_count, task_text=task_text)
            if len(style_cards) != self.policy.candidate_count:
                raise ValueError("风格库未返回策略要求数量的已批准风格卡。")
        except Exception as exc:
            self._handle_skill_failure("style_library", exc, degraded_reasons)

        # 使用 StyleIdeaGenerator 生成包含【构图、材质、推荐理由、主要风险】的文本卡片
        from interaction.approval_gate import TaskConfirmationDoc
        doc = TaskConfirmationDoc(task_id=task_card.task_id, confirmed_facts=[], default_handling_for_unknowns=[])
        idea_cards = StyleIdeaGenerator(offline_mode=self.offline_mode).generate(
            task_card=task_card, confirmation_doc=doc, style_cards=style_cards, count=self.policy.candidate_count
        )

        # 3. 终端输出这 5 张文本卡片给用户
        self.output("\n=================== 🎨 筛选出 5 种艺术风格方向 ===================")
        for i, idea in enumerate(idea_cards, 1):
            self.output(f"\n【方向 {i}：{idea.title}】")
            self.output(f"  • 构图机制：{idea.composition}")
            self.output(f"  • 材质语言：{idea.material}")
            self.output(f"  • 推荐理由：{idea.fit_reason}")
            self.output(f"  • 艺术理念：{idea.artistic_philosophy}")
            self.output(f"  • 可借鉴机制：{idea.adaptable_mechanism}")
            self.output(f"  • 主要风险：{idea.major_risk}")
        self.output("\n=================================================================")
        self.output("保持主体内容、品牌色彩与空间条件一致，正在按上述 5 种风格分别生图，请稍候...\n")

        # 4. 生图逻辑：固定内容与品牌色，只注入各自风格
        def render(index: int) -> dict[str, Any]:
            idea = idea_cards[index] if index < len(idea_cards) else None
            prompt = (
                f"商业效果图绘制。\n"
                f"【项目主体与内容规范】\n{specification_to_markdown(spec)}\n"
                f"【艺术风格机制】\n"
                f"风格名称：{idea.title if idea else ''}\n"
                f"构图分布：{idea.composition if idea else ''}\n"
                f"材质光影：{idea.material if idea else ''}\n"
                f"【品类规范】\n{category_skill.prompt_injection.category_description if category_skill else ''}\n"
                f"要求：保持项目主体与品牌色彩一致，呈现选定的艺术风格机制。"
            )
            result = self._image_call("initial_candidate_generation", prompt, [], index=index)
            return {**normalize_image_asset(result), "candidate_index": index, "id": f"candidate-{index + 1}", "style_name": idea.title if idea else f"方向 {index + 1}"}

        batch = CandidateBatchGenerator(self.store, render, attempts=self.policy.max_render_retries + 1,
                                        max_workers=self.policy.candidate_concurrency).generate(
                                            spec.content_hash, count=self.policy.candidate_count)
        if batch["failed"]: 
            raise RuntimeError(f"候选图有 {len(batch['failed'])} 项超时失败；成功项已保存，运行 resume 可重试。")
        return {"candidates": batch["succeeded"], "style_idea_cards": [c.model_dump(mode="json") for c in idea_cards],
                "skill_status": "degraded" if degraded_reasons else "ready",
                "skill_degradation_reasons": degraded_reasons}

    def _handle_skill_failure(self, skill: str, exc: Exception, reasons: list[dict[str, str]]) -> None:
        reason = {"skill": skill, "code": type(exc).__name__, "message": str(exc)}
        if self.policy.skill_failure_mode != "allow_degraded":
            self.store.events.append("skill_load_blocked", **reason, retryable=True)
            raise SkillLoadError(f"必需 Skill {skill} 加载失败：{exc}") from exc
        reasons.append(reason)
        self.store.events.append("skill_degraded", **reason, visible=True)
        self.output(f"警告：Skill {skill} 已按运行策略降级：{exc}")

    def _selection(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        selected = options.get("selected_id")
        if not selected: return {"waiting": True, "phase": "waiting_master_selection"}
        return {"master_asset": self.workflow.select_master(data["candidates"], selected), "waiting": False, "phase": "master_selected"}

    def _self_check(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        spec = TaskSpecification.model_validate(data["task_specification"])
        policy_data = data.get("self_check_policy", self.policy.self_check.model_dump(mode="json"))
        policy_data = dict(policy_data)
        policy_data["max_rounds"] = min(int(policy_data.get("max_rounds", self.policy.self_check.max_rounds)),
                                        self.policy.max_calibration_retries + 1)
        loop = CalibrationLoop(self.store, SelfCheckPolicy(**policy_data), inspector=self._inspect,
            reworker=lambda assembled: self._image_call("self_check_rework", assembled["text"], [r["uri"] for r in assembled["references"]]),
            presenter=lambda number, result: self._present_inspection(number, result))
        action = options.get("manual_action")
        result = loop.run(current_asset=data.get("asset") or data["master_asset"], stable_specification=specification_to_markdown(spec),
                          constraints=[], approve=(lambda _: action) if action else None,
                          start_round=int(data.get("round", 1)))
        return {**result, "current_asset": result.get("asset", data.get("master_asset"))}
    
    def _human_rework(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        prompt = options.get("human_prompt")
        if not prompt: return {"asset": data.get("current_asset") or data.get("asset"), "waiting": False}
        current = data.get("current_asset") or data["asset"]
        result = self._image_call("human_prompt_rework", prompt, [str(current["uri"])])
        asset = normalize_image_asset(result)
        self.store.events.append("calibration_invalidated", reason="human_rework", previous_checked_asset_hash=data.get("latest_checked_asset_hash"), new_asset_hash=asset["sha256"])
        return {"asset": asset, "current_asset": asset, "waiting": True, "phase": "waiting_reinspection", "calibration_status": "invalidated",
                "termination_satisfied": False, "termination_reason": "asset_changed_after_human_rework",
                "latest_checked_asset_hash": None, "inspection": None}

    def _present_inspection(self, number: int, result: VisualCheckResult) -> None:
        self.store.events.append("inspection_presented", round=number, result=result.model_dump(mode="json"))
        self.output(f"第 {number} 轮画面质检\n{self.presenter.inspection(result)}")

    def _final(self, data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        asset = data.get("asset") or data.get("current_asset")
        checked_hash = data.get("latest_checked_asset_hash")
        satisfied = bool(data.get("termination_satisfied"))
        if data.get("calibration_status") not in {"completed", "human_accepted"}:
            satisfied = False
        if not checked_hash or checked_hash != asset.get("sha256"):
            satisfied = False
        if not data.get("selected_policy") or not data.get("termination_reason"):
            satisfied = False
        action = options.get("final_action")
        if action == "continue":
            self.store.events.append("final_confirmation_declined", asset_sha256=asset.get("sha256"),
                                     actor=options.get("actor"))
            return {"waiting": True, "phase": "waiting_human_rework", "completed": False,
                    "final_confirmation": None}
        if action != "confirm":
            return {"waiting": True, "phase": "waiting_final_confirmation", "completed": False}
        actor = str(options.get("actor") or "").strip()
        if not actor:
            raise ValueError("最终确认必须提供 actor。")
        self.workflow.validate_final_asset(asset, human_approved=True, self_check_complete=satisfied)
        approval = {"asset_sha256": asset["sha256"], "actor": actor,
                    "confirmed_at": datetime.now(timezone.utc).isoformat()}
        self.store.events.append("final_asset_confirmed", **approval)
        return {"final_asset": asset, "final_confirmation": approval, "completed": True,
                "delivery_frozen": True, "waiting": False, "phase": "delivery_frozen"}

    def _text(self, route: ModelRoute):
        client = build_text_client(route.binding)
        if client is None: raise RuntimeError("文本模型不可用。")
        return client

    def _inspect(self, image_uri: str, prompt: str) -> dict[str, Any]:
            if self.offline_mode:
                return {"passed": False, "decision": "continue", "rework_prompt_delta": "提高主体清晰度", "confidence": 0.8}
            
            # 👇 构造明确要求返回 JSON 的质检 Prompt
            inspection_prompt = (
                f"你是一个专业的视觉质量审查员。请对比任务书与图片，检查图片是否符合要求。\n"
                f"只能输出一个纯 JSON 对象，格式要求如下：\n"
                f"{{\n"
                f'  "passed": true或false,\n'
                f'  "decision": "pass"（符合）或 "continue"（需微调）或 "blocked"（严重不符）,\n'
                f'  "deviations": ["发现的问题或偏差描述"],\n'
                f'  "rework_prompt_delta": "如果不符合，给出具体的改图提示词建议",\n'
                f'  "confidence": 0.95\n'
                f"}}\n\n"
                f"设计任务书要求：\n{prompt}"
            )
            
            return self.gateway.call("self_check_inspection", ModelRole.VISION_LANGUAGE_MODEL,
                lambda route: self._vlm(route).inspect(image_uri, inspection_prompt), 
                messages=[{"role":"user","content":inspection_prompt},{"role":"image","content":image_uri}],
                variables={"image":image_uri}, template_id="visual-check", template_version="2", input_refs=[image_uri], needs_images=1)

    def _vlm(self, route: ModelRoute):
        client = build_vlm_client(route.binding)
        if client is None: raise RuntimeError("视觉检查模型不可用。")
        return client

    def _image_call(self, state: str, prompt: str, references: list[str], *, index: int | None = None) -> dict[str, Any]:
        if self.offline_mode:
            # Still cross the Gateway, so offline tests exercise routing/auditing.
            result = self.gateway.call(state, ModelRole.TEXT_TO_IMAGE_MODEL,
                lambda route: {"uri": f"mock://{state}/{index or 0}", "mock": True, "provider": "offline", "model": route.binding.model},
                messages=[{"role":"user","content":prompt}], variables={"candidate_index":index, "reference_images":references},
                template_id=state, template_version="2", input_refs=references, needs_images=len(references))
            return normalize_image_asset(result)
        result = self.gateway.call(state, ModelRole.TEXT_TO_IMAGE_MODEL,
            lambda route: ArkImageRenderClient(base_url=self.policy.image_api_base_url or None,
                model=route.binding.model).render(build_render_payload(route.binding.model, prompt,
                self.policy.default_output_size, {"state":state},
                response_format=self.policy.response_format, watermark=self.policy.watermark, reference_images=references)),
            messages=[{"role":"user","content":prompt}], variables={"candidate_index":index, "reference_images":references},
            template_id=state, template_version="2", input_refs=references, needs_images=len(references))
        return persist_image_asset(result, self.store.artifacts)

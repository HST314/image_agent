"""Explicit workflow transitions and orthogonal self-check policies."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, TypedDict


class StateDefinition(TypedDict):
    handler: str
    implementation: str
    actions: tuple[str, ...]
    gate: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    paid: bool
    reentry: str
    successors: tuple[str, ...]


# Canonical product-state catalogue. Runtime handlers may keep coarse legacy
# names, but UI/action contracts and migrations derive from this sole source.
STATE_DEFINITIONS: dict[str, StateDefinition] = {
    "received": {"actions": ("start_clarification",), "gate": "valid_immutable_inbound", "inputs": ("design_task_envelope",), "outputs": ("task_card",), "paid": False, "reentry": "inbound_idempotency_key", "successors": ("clarifying",)},
    "clarifying": {"actions": ("answer_clarification",), "gate": "clarification_budget", "inputs": ("task_card",), "outputs": ("clarification_answers",), "paid": False, "reentry": "question_fingerprint", "successors": ("task_spec_building",)},
    "task_spec_building": {"actions": ("build_spec",), "gate": "clarification_resolved", "inputs": ("clarification_answers",), "outputs": ("task_specification",), "paid": False, "reentry": "task_spec_content_hash", "successors": ("waiting_task_spec_confirmation",)},
    "waiting_task_spec_confirmation": {"actions": ("edit_task_spec", "confirm_task_spec"), "gate": "human_decision", "inputs": ("task_specification",), "outputs": ("task_spec_confirmation",), "paid": False, "reentry": "confirmation_subject_hash", "successors": ("task_spec_building", "category_analysis")},
    "category_analysis": {"actions": ("analyze_category",), "gate": "valid_confirmation_and_p0_paid_gate", "inputs": ("confirmed_task_spec",), "outputs": ("category_constraints",), "paid": True, "reentry": "confirmed_spec_hash", "successors": ("style_selection_vlm",)},
    "style_selection_vlm": {"actions": ("select_and_interpret_styles",), "gate": "five_legal_styles_and_p0_paid_gate", "inputs": ("confirmed_task_spec", "style_library"), "outputs": ("style_slot_audit",), "paid": True, "reentry": "spec_and_library_hash", "successors": ("five_candidate_generation",)},
    "five_candidate_generation": {"actions": ("generate_candidates", "retry_failed_slots"), "gate": "five_valid_vlm_outputs_and_p0_paid_gate", "inputs": ("style_slot_audit",), "outputs": ("five_candidates",), "paid": True, "reentry": "slot_idempotency_key", "successors": ("waiting_master_selection",)},
    "waiting_master_selection": {"actions": ("select_master",), "gate": "human_decision", "inputs": ("five_candidates",), "outputs": ("master_asset",), "paid": False, "reentry": "decision_idempotency_key", "successors": ("quality_rework",)},
    "quality_rework": {"actions": ("inspect", "rework"), "gate": "p0_paid_gate_and_round_limit", "inputs": ("master_or_reworked_asset",), "outputs": ("inspection", "reworked_asset"), "paid": True, "reentry": "asset_hash_and_round", "successors": ("quality_rework", "waiting_human_decision", "waiting_final_confirmation")},
    "waiting_human_decision": {"actions": ("continue_generation", "manual_rework", "abandon"), "gate": "human_decision", "inputs": ("failed_checks",), "outputs": ("quality_disposition",), "paid": False, "reentry": "decision_idempotency_key", "successors": ("quality_rework", "human_rework", "terminated")},
    "human_rework": {"actions": ("human_rework",), "gate": "human_prompt_or_guidance_and_p0_paid_gate", "inputs": ("current_asset", "guidance"), "outputs": ("reworked_asset",), "paid": True, "reentry": "asset_guidance_prompt_hash", "successors": ("reinspection", "waiting_final_confirmation")},
    "reinspection": {"actions": ("resume",), "gate": "p0_paid_gate", "inputs": ("reworked_asset",), "outputs": ("inspection",), "paid": True, "reentry": "asset_hash_and_round", "successors": ("waiting_human_decision", "waiting_final_confirmation")},
    "waiting_final_confirmation": {"actions": ("confirm_final", "continue_modifying"), "gate": "human_decision_and_latest_inspection", "inputs": ("checked_asset",), "outputs": ("final_confirmation",), "paid": False, "reentry": "confirmation_subject_hash", "successors": ("human_rework", "delivery_frozen")},
    "delivery_frozen": {"actions": ("generate_note",), "gate": "valid_final_confirmation", "inputs": ("final_asset",), "outputs": ("frozen_delivery",), "paid": False, "reentry": "delivery_content_hash", "successors": ("delivery_return",)},
    "delivery_return": {"actions": ("generate_note", "manual_return"), "gate": "frozen_delivery", "inputs": ("frozen_delivery",), "outputs": ("design_note", "return_receipt"), "paid": False, "reentry": "delivery_version", "successors": ()},
    "terminated": {"actions": (), "gate": "none", "inputs": ("quality_disposition",), "outputs": (), "paid": False, "reentry": "none", "successors": ()},
}

# Handler ownership is stored on each canonical state rather than in runner/UI
# side tables. Assignment is kept adjacent to the catalogue for readability.
for _state, _handler, _implementation in (
    ("received", "intake_clarify", "_clarify"),
    ("clarifying", "intake_clarify", "_clarify"),
    ("task_spec_building", "confirmation_build", "_confirmation"),
    ("waiting_task_spec_confirmation", "confirmation_build", "_confirmation"),
    ("category_analysis", "initial_candidate_generation", "_candidates"),
    ("style_selection_vlm", "initial_candidate_generation", "_candidates"),
    ("five_candidate_generation", "initial_candidate_generation", "_candidates"),
    ("waiting_master_selection", "master_candidate_selection", "_selection"),
    ("quality_rework", "self_check_iteration", "_self_check"),
    ("waiting_human_decision", "self_check_iteration", "_self_check"),
    ("human_rework", "human_prompt_iteration", "_human_rework"),
    ("reinspection", "self_check_iteration", "_self_check"),
    ("waiting_final_confirmation", "final_approval", "_final"),
    ("delivery_frozen", "final_approval", "_final"),
    ("delivery_return", "final_approval", "_final"),
    ("terminated", "self_check_iteration", "_self_check"),
):
    STATE_DEFINITIONS[_state]["handler"] = _handler
    STATE_DEFINITIONS[_state]["implementation"] = _implementation


WAITING_STATES = frozenset(name for name in STATE_DEFINITIONS if name.startswith("waiting_"))


def allowed_actions(state: str) -> tuple[str, ...]:
    try:
        return STATE_DEFINITIONS[state]["actions"]
    except KeyError as exc:
        raise InvalidTransitionError(f"未知产品状态：{state}") from exc


def handler_for(state: str) -> str:
    try:
        return STATE_DEFINITIONS[state]["handler"]
    except KeyError as exc:
        raise InvalidTransitionError(f"未知产品状态：{state}") from exc


def validate_product_successor(current: str, target_handler: str) -> None:
    definition = STATE_DEFINITIONS.get(current)
    if definition is None or not definition["gate"]:
        raise InvalidTransitionError(f"产品状态 {current!r} 缺少有效 gate。")
    permitted = {definition["handler"], *(handler_for(state) for state in definition["successors"])}
    if target_handler not in permitted:
        raise InvalidTransitionError(f"产品状态 {current!r} 不允许进入处理器 {target_handler!r}。")


def legacy_handler_order() -> tuple[str, ...]:
    return tuple(dict.fromkeys(definition["handler"] for definition in STATE_DEFINITIONS.values()))


LEGACY_PHASE_STATE_MAP: dict[tuple[str, str], str] = {
    ("intake_clarify", "waiting_clarification"): "clarifying",
    ("intake_clarify", "clarification_completed"): "task_spec_building",
    ("intake_clarify", "clarification_round_limit_reached"): "task_spec_building",
    ("confirmation_build", "waiting_task_spec_confirmation"): "waiting_task_spec_confirmation",
    ("confirmation_build", "task_spec_confirmed"): "category_analysis",
    ("initial_candidate_generation", "waiting_master_selection"): "waiting_master_selection",
    ("master_candidate_selection", "master_selected"): "quality_rework",
    ("self_check_iteration", "waiting_quality_disposition"): "waiting_human_decision",
    ("self_check_iteration", "waiting_human_approval"): "waiting_human_decision",
    ("self_check_iteration", "waiting_final_confirmation"): "waiting_final_confirmation",
    ("self_check_iteration", "round_checkpointed"): "quality_rework",
    ("human_prompt_iteration", "waiting_human_rework"): "human_rework",
    ("human_prompt_iteration", "waiting_reinspection"): "reinspection",
    ("final_approval", "waiting_final_confirmation"): "waiting_final_confirmation",
    ("final_approval", "delivery_frozen"): "delivery_frozen",
}


def project_execution_cursor(legacy_state: str, data: dict[str, object]) -> dict[str, object] | None:
    """Project a legacy handler checkpoint onto the canonical product state.

    This is deliberately pure: loading an old checkpoint never rewrites it.
    """
    phase = str(data.get("phase") or "")
    product_state = LEGACY_PHASE_STATE_MAP.get((legacy_state, phase))
    if product_state is None and legacy_state == "intake_clarify" and data.get("waiting") is False:
        product_state = "task_spec_building"
    if product_state is None and legacy_state == "confirmation_build" and data.get("task_spec_confirmation"):
        product_state = "category_analysis"
    if product_state is None and legacy_state == "initial_candidate_generation" and data.get("candidates"):
        product_state = "waiting_master_selection"
    if product_state is None and legacy_state == "master_candidate_selection" and data.get("master_asset"):
        product_state = "quality_rework"
    if product_state is None and legacy_state == "final_approval" and data.get("delivery_frozen"):
        product_state = "delivery_frozen"
    if product_state is None:
        # Handler-level fallback covers old checkpoints written before `phase`
        # became mandatory. It is conservative and never advances paid work.
        product_state = {
            "received": "received",
            "intake_clarify": "clarifying",
            "confirmation_build": "task_spec_building",
            "initial_candidate_generation": "five_candidate_generation",
            "master_candidate_selection": "waiting_master_selection",
            "self_check_iteration": "quality_rework",
            "human_prompt_iteration": "human_rework",
            "final_approval": "waiting_final_confirmation",
        }.get(legacy_state)
    if product_state is None:
        return None
    return {"version": 1, "product_state": product_state, "handler": legacy_state,
            "unit": phase or legacy_state}

TransitionMap = dict[str, frozenset[str]]


def legacy_transitions() -> TransitionMap:
    edges: dict[str, set[str]] = {}
    for definition in STATE_DEFINITIONS.values():
        source = definition["handler"]
        edges.setdefault(source, set())
        for successor in definition["successors"]:
            target = handler_for(successor)
            if target != source or successor in {"quality_rework", "human_rework"}:
                edges[source].add(target)
    return {source: frozenset(targets) for source, targets in edges.items()}

class InvalidTransitionError(ValueError):
    pass

def validate_transition(current: str, target: str) -> None:
    if target not in legacy_transitions().get(current, frozenset()):
        raise InvalidTransitionError(f"不能从“{current}”直接进入“{target}”。")

@dataclass(frozen=True)
class SelfCheckPolicy:
    termination: Literal["fix", "solo"]
    release: Literal["manual", "auto"]
    fixed_rounds: int = 1
    max_rounds: int = 3
    stop_early_on_pass: bool = False

    def should_stop(self, *, round_number: int, decision: Literal["continue", "pass", "blocked"]) -> bool:
        if decision == "blocked":
            return True
        if self.termination == "fix":
            return (self.stop_early_on_pass and decision == "pass") or round_number >= self.fixed_rounds
        return decision == "pass" or round_number >= self.max_rounds

    def needs_human_release(self) -> bool:
        return self.release == "manual"

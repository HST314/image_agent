from agent_core.workflow import STATE_DEFINITIONS, WAITING_STATES, allowed_actions


def test_every_product_state_has_complete_executable_contract():
    expected = {
        "received", "clarifying", "task_spec_building", "waiting_task_spec_confirmation",
        "category_analysis", "style_selection_vlm", "five_candidate_generation",
        "waiting_master_selection", "quality_rework", "waiting_human_decision",
        "human_rework", "reinspection", "waiting_final_confirmation", "delivery_frozen",
        "delivery_return",
    }
    assert expected <= STATE_DEFINITIONS.keys()
    for name, definition in STATE_DEFINITIONS.items():
        assert set(definition) == {"actions", "gate", "inputs", "outputs", "paid", "reentry", "successors"}
        assert all(successor in STATE_DEFINITIONS for successor in definition["successors"])
        assert allowed_actions(name) == definition["actions"]


def test_waiting_states_are_unpaid_human_gates_not_failures():
    assert WAITING_STATES
    assert all(not STATE_DEFINITIONS[state]["paid"] for state in WAITING_STATES)
    assert all("human" in STATE_DEFINITIONS[state]["gate"] for state in WAITING_STATES)


def test_all_paid_states_reuse_server_gate():
    paid = [definition for definition in STATE_DEFINITIONS.values() if definition["paid"]]
    assert paid and all("p0_paid_gate" in definition["gate"] for definition in paid)

"""Eight natural forces as substrate physics, not a persona org chart."""

from __future__ import annotations

import pytest

from ai_nature_company.forces import (
    FORCE_NAMES,
    LEDGER_HANDOFF,
    SCALES,
    describe_effectors,
    describe_natural_forces,
    work_kind_force,
)

from .scenario import Scenario
from .support import CELL_ID, stable_uuid

SENSE_WORK_ID = stable_uuid("sense-market-work")
SENSE_EVIDENCE_ID = stable_uuid("sense-market-evidence")
SENSE_FAIL_WORK_ID = stable_uuid("sense-fail-work")
ACT_WORK_ID = stable_uuid("act-sales-work")
ACT_EVIDENCE_ID = stable_uuid("act-sales-evidence")
ACT_FAIL_WORK_ID = stable_uuid("act-fail-work")


async def _tries_running_experiment() -> Scenario:
    scenario = Scenario()
    await scenario.bootstrap(
        constitution_unit="tries",
        constitution_currency=None,
        company_limit_minor=20,
        cell_limit_minor=10,
        experiment_limit_minor=4,
        single_action_limit_minor=4,
        budget_unit="tries",
        budget_currency=None,
        budget_limit=20,
    )
    await scenario.create_cell(budget_limit=10, unit="tries", currency=None)
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()
    await scenario.register_experiment(budget={"unit": "tries", "amount_minor": 4})
    await scenario.start_experiment()
    return scenario


def _cell_ledger(described: dict) -> dict:
    portfolio = described["portfolio"]
    cell = next(item for item in portfolio["cells"] if item["cell_id"] == CELL_ID)
    ledger = next(
        row
        for row in portfolio["remaining_budget_by_unit"]
        if row.get("scope") == "cell" and row.get("cell_id") == CELL_ID
    )
    return {"cell": cell, "ledger": ledger}


def test_work_kind_force_splits_sense_from_act() -> None:
    assert work_kind_force("sense_market") == "sense"
    assert work_kind_force("scout_market") == "act"
    assert work_kind_force("sales_step") == "act"
    assert work_kind_force("delivery_step") == "act"
    assert work_kind_force("collect_verified_sample") == "act"


def test_natural_forces_are_physics_not_personas() -> None:
    described = describe_natural_forces()
    assert described["kind"] == "physics"
    assert described["mcp_is"] == "physics_engine"
    assert described["hermetic"] == "as_above_so_below"
    assert described["names"] == list(FORCE_NAMES)
    assert described["scales"] == list(SCALES)
    assert "roles" in described["not"]
    assert "chat_personas" in described["not"]
    for name in FORCE_NAMES:
        force = described["forces"][name]
        assert set(SCALES) <= set(force)
    effectors = describe_effectors()
    assert effectors["not"] == "the_eight_forces"
    sit = effectors["sit_the_seat"]
    assert sit["live_principals"] == ["owner", "specialist", "evaluator"]
    assert sit["owner"]["effectuates"] == "bound"
    assert sit["owner"]["determines"] == ["icp", "targets"]
    assert sit["owner"]["does_not_impersonate"] == "select"
    assert sit["sense"]["effectuates"] == "sense"
    assert sit["sense"]["work_kind"] == "sense_market"
    assert sit["sense"]["debits_try_spend"] is False
    assert sit["sense"]["path"] == "second_specialist_effector_path"
    assert sit["sense"]["not_a_new_force"] is True
    assert "sensing_must_use" not in sit["hunter"]
    assert sit["hunter"]["effectuates"] == "act"
    assert sit["hunter"]["requires"] == "sense_ready_packets"
    assert sit["hunter"]["external_send"] == "founder_sign_off"
    assert sit["evaluator"]["effectuates"] == "defend"
    assert described["handoff"] == dict(LEDGER_HANDOFF)
    assert described["handoff"]["default"] == "write_ledger_then_next_force_reads"
    assert "peer_dm" in described["handoff"]["not"]
    assert "send_to_agent" in described["handoff"]["not"]
    assert described["handoff"]["does_not_mean"] == "silent_to_everyone"
    assert described["handoff"]["external_entity_chat"] == (
        "normal_and_distinct_from_force_peer_silence"
    )
    assert described["handoff"]["external_entities_are_not"] == "anc_ledger_peers"
    assert described["handoff"]["host_MUST_when_external_waiting"] == [
        "acknowledge",
        "do_the_work",
        "wake_or_reply",
    ]
    assert sit["handoff"]["send_to_agent_for_routine_handoffs"] is False
    assert sit["handoff"]["does_not_mean"] == "silent_to_everyone"
    assert sit["handoff"]["external_entity_chat"] == "normal"
    assert sit["handoff"]["surface_to_founder_at"] == [
        "sign",
        "pause",
        "icp_change",
        "budget",
        "activation_mismatch",
    ]


@pytest.mark.asyncio
async def test_protocol_describe_advertises_eight_forces_as_physics() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["owner"],
        {},
    )
    forces = described["natural_forces"]
    assert forces == describe_natural_forces()
    assert forces["names"] == [
        "bound",
        "sense",
        "act",
        "transform",
        "maintain",
        "defend",
        "select",
        "continue",
    ]
    assert forces["kind"] == "physics"
    assert forces["forces"]["sense"]["debits_try_spend"] is False
    assert forces["forces"]["act"]["debits_try_spend"] is True
    assert forces["forces"]["transform"]["select_reads"] == "fitness_evidence_count"
    assert forces["forces"]["defend"]["producer_equals_verifier"] is False
    assert described["signed_cast_are"] == "optional_effectors_inside_forces"
    assert described["effectors"]["not"] == "the_eight_forces"
    assert described["runtime_tick"]["forces"] == ["maintain", "continue"]
    assert described["cell_routines"]["reflex"]["forces"] == ["sense", "act"]
    assert "sense_market" in described["cell_routines"]["reflex"]["sense_work_kinds"]
    workflow = described["bet_workflow"]["forces"]
    assert workflow["sense_does_not_debit_try_spend"] is True
    assert workflow["act_complete_fail_debits_try_spend"] is True
    assert described["bet_workflow"]["handoff"] == dict(LEDGER_HANDOFF)
    assert described["cell_routines"]["reflex"]["handoff"] == ("write_ledger_then_next_force_reads")
    assert "peer_dm" in described["natural_forces"]["handoff"]["not"]
    assert described["natural_forces"]["handoff"]["does_not_mean"] == "silent_to_everyone"
    assert described["natural_forces"]["handoff"]["external_entity_chat"] == (
        "normal_and_distinct_from_force_peer_silence"
    )
    assert described["effectors"]["sit_the_seat"]["handoff"]["external_entity_chat"] == ("normal")


@pytest.mark.asyncio
async def test_sense_work_does_not_spend_tries_act_complete_and_fail_do() -> None:
    scenario = await _tries_running_experiment()
    before = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert before["ledger"]["spent_minor"] == 0
    assert before["ledger"]["reserved_minor"] == 4

    await scenario.create_work(
        work_item_id=SENSE_WORK_ID,
        kind="sense_market",
        budget={"unit": "tries", "maximum_cost_minor": 1},
        key="create-sense-work",
    )
    await scenario.claim_work(work_item_id=SENSE_WORK_ID, key="claim-sense-work")
    await scenario.append_evidence(
        evidence_id=SENSE_EVIDENCE_ID,
        evidence_type="other",
        observation={
            "value": 1,
            "unit": "demand_hint",
            "summary": "Observed a customer statement without changing the world.",
        },
        key="append-sense-evidence",
    )
    completed = await scenario.complete_work(
        work_item_id=SENSE_WORK_ID,
        evidence_id=SENSE_EVIDENCE_ID,
        actual_cost={"unit": "tries", "amount_minor": 1},
        key="complete-sense-work",
    )
    assert completed["state"]["work_item"]["kind"] == "sense_market"

    after_sense = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_sense["ledger"]["spent_minor"] == 0
    assert after_sense["ledger"]["reserved_minor"] == 4
    assert after_sense["cell"]["activity_evidence_count"] == 1
    assert after_sense["cell"]["fitness_evidence_count"] == 0

    await scenario.create_work(
        work_item_id=SENSE_FAIL_WORK_ID,
        kind="sense_market",
        budget={"unit": "tries", "maximum_cost_minor": 1},
        key="create-sense-fail-work",
    )
    await scenario.claim_work(work_item_id=SENSE_FAIL_WORK_ID, key="claim-sense-fail-work")
    await scenario.fail_work(
        work_item_id=SENSE_FAIL_WORK_ID,
        actual_cost={"unit": "tries", "amount_minor": 1},
        key="fail-sense-work",
    )
    after_sense_fail = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_sense_fail["ledger"]["spent_minor"] == 0
    assert after_sense_fail["ledger"]["reserved_minor"] == 4

    await scenario.create_work(
        work_item_id=ACT_WORK_ID,
        kind="sales_step",
        budget={"unit": "tries", "maximum_cost_minor": 1},
        key="create-act-work",
    )
    await scenario.claim_work(work_item_id=ACT_WORK_ID, key="claim-act-work")
    await scenario.append_evidence(
        evidence_id=ACT_EVIDENCE_ID,
        evidence_type="other",
        observation={
            "value": 1,
            "unit": "packet_sent",
            "summary": "Sales sent one first-touch packet.",
        },
        key="append-act-evidence",
    )
    await scenario.complete_work(
        work_item_id=ACT_WORK_ID,
        evidence_id=ACT_EVIDENCE_ID,
        actual_cost={"unit": "tries", "amount_minor": 1},
        key="complete-act-work",
    )
    after_act = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_act["ledger"]["spent_minor"] == 1
    assert after_act["ledger"]["reserved_minor"] == 3
    assert after_act["cell"]["spent_minor"] == 1

    await scenario.create_work(
        work_item_id=ACT_FAIL_WORK_ID,
        kind="delivery_step",
        budget={"unit": "tries", "maximum_cost_minor": 2},
        key="create-act-fail-work",
    )
    await scenario.claim_work(work_item_id=ACT_FAIL_WORK_ID, key="claim-act-fail-work")
    await scenario.fail_work(
        work_item_id=ACT_FAIL_WORK_ID,
        actual_cost={"unit": "tries", "amount_minor": 2},
        key="fail-act-work",
    )
    after_act_fail = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_act_fail["ledger"]["spent_minor"] == 3
    assert after_act_fail["ledger"]["reserved_minor"] == 1
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    assert viewed["spent_minor"] == 3
    assert viewed["fitness_evidence_count"] == 0
    assert viewed["activity_evidence_count"] == 2

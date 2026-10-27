"""Owner-led operational allocation and containment without allocator or immune."""

from __future__ import annotations

from copy import deepcopy

import pytest

from ai_nature_company.auth import Role
from ai_nature_company.errors import AuthorizationError

from .scenario import REQUIRED_GATES, Scenario
from .support import (
    CELL_ID,
    COMPANY_ID,
    EVALUATION_ID,
    PRINCIPAL_IDS,
    make_principal,
    stable_uuid,
)


def _state_record(response: dict, key: str) -> dict:
    state = response["state"]
    if key in state:
        return dict(state[key])
    return dict(state)


async def _owner_led_evaluated_company() -> Scenario:
    scenario = Scenario()
    await scenario.bootstrap()
    await scenario.build_evaluated_experiment()
    return scenario


@pytest.mark.asyncio
async def test_cell_owner_can_fund_and_maintain_its_bound_cell() -> None:
    scenario = await _owner_led_evaluated_company()
    target = {"kind": "cell", "id": CELL_ID, "revision": scenario.cell_revision}

    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    assert "allocation.commit" in described["principal"]["effective_capabilities"]
    assert "evaluation.submit" not in described["principal"]["effective_capabilities"]
    ledgers = described["portfolio"]["remaining_budget_by_unit"]
    assert ledgers
    assert all("unit" in row and "available_minor" in row for row in ledgers)
    assert all("packets_sent" not in row for row in ledgers)

    gate = await scenario.invoke(
        "gate.evaluate",
        "owner",
        {
            "action": "fund",
            "stage": "exploitation",
            "target": target,
            "evaluation_id": EVALUATION_ID,
            "amount": {"currency": "USD", "amount_minor": 9_000},
        },
        expected_revision=0,
        key="owner-fund-gates",
    )
    decision = _state_record(gate, "decision")
    assert decision["passed"] is True
    assert {item["gate"] for item in decision["gate_results"]} == set(REQUIRED_GATES)

    allocation_id = stable_uuid("owner-fund-allocation")
    proposed = await scenario.invoke(
        "allocation.propose",
        "owner",
        {
            "allocation_id": allocation_id,
            "action": "fund",
            "target": target,
            "stage": "exploitation",
            "gate_decision_id": decision["decision_id"],
            "ranking": {
                "formula": "exploitation-v1",
                "score_numerator": 1,
                "score_denominator": 999,
                "input_snapshot": {
                    "expected_12m_contribution_profit_minor": 500_000,
                    "evidence_confidence_bps": 9_000,
                    "expected_repeatability_bps": 6_000,
                    "capital_at_risk_minor": 10_000,
                    "time_to_cash_days": 30,
                },
            },
            "amount": {"currency": "USD", "amount_minor": 9_000},
            "rationale_summary": "Owner commits bounded exploitation capital for this cell.",
        },
        expected_revision=0,
        key="owner-propose-fund",
    )
    committed = await scenario.invoke(
        "allocation.commit",
        "owner",
        {"allocation_id": allocation_id},
        expected_revision=int(proposed["state"]["allocation"]["revision"]),
        key="owner-commit-fund",
    )
    allocation = _state_record(committed, "allocation")
    assert allocation["decision_state"] == "committed"
    assert allocation["action"] == "fund"
    assert allocation["decided_by_principal_id"] == PRINCIPAL_IDS["owner"]
    assert committed["state"]["reservation"] is not None


@pytest.mark.asyncio
async def test_cell_owner_can_pause_its_cell_without_immune_or_allocator() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    before = await scenario.store.export_company(COMPANY_ID)
    allocator_events = [
        event
        for event in before["domain_events"]
        if event.get("actor_id") == PRINCIPAL_IDS["allocator"]
    ]
    immune_events = [
        event
        for event in before["domain_events"]
        if event.get("actor_id") == PRINCIPAL_IDS["immune"]
    ]
    assert allocator_events == []
    assert immune_events == []

    paused = await scenario.invoke(
        "cell.transition",
        "owner",
        {"cell_id": CELL_ID, "target_status": "paused"},
        expected_revision=scenario.cell_revision,
        key="owner-pause-cell",
    )
    cell = _state_record(paused, "cell")
    assert cell["status"] == "paused"

    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    assert viewed["cell"]["status"] == "paused"
    assert viewed["eligibility"]["pause_eligible"] is False
    assert viewed["eligibility"]["kill_eligible"] is True
    assert viewed["eligibility"]["kill_requires_founder"] is True


@pytest.mark.asyncio
async def test_cell_owner_can_commit_owner_authorized_pause_allocation() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    target = {"kind": "cell", "id": CELL_ID, "revision": scenario.cell_revision}
    gate = await scenario.invoke(
        "gate.evaluate",
        "owner",
        {
            "action": "pause",
            "stage": "containment",
            "target": target,
            "containment_basis": {
                "kind": "cell_owner_authorized",
                "summary": "Pause this cell after the frozen stop window.",
            },
        },
        expected_revision=0,
        key="owner-pause-gate",
    )
    decision = _state_record(gate, "decision")
    assert decision["containment_basis"]["kind"] == "cell_owner_authorized"
    assert decision["containment_basis"]["authorized_by_principal_id"] == PRINCIPAL_IDS["owner"]

    allocation_id = stable_uuid("owner-pause-allocation")
    proposed = await scenario.invoke(
        "allocation.propose",
        "owner",
        {
            "allocation_id": allocation_id,
            "action": "pause",
            "target": target,
            "stage": "containment",
            "gate_decision_id": decision["decision_id"],
            "ranking": {"formula": "not-ranked", "score_numerator": 0, "score_denominator": 1},
            "amount": None,
            "rationale_summary": "Owner pauses the cell under the frozen containment path.",
        },
        expected_revision=0,
        key="owner-propose-pause",
    )
    committed = await scenario.invoke(
        "allocation.commit",
        "owner",
        {"allocation_id": allocation_id},
        expected_revision=int(proposed["state"]["allocation"]["revision"]),
        key="owner-commit-pause",
    )
    assert committed["state"]["allocation"]["decision_state"] == "committed"
    assert committed["state"]["cell"]["status"] == "paused"
    assert committed["state"]["reservation"] is None


@pytest.mark.asyncio
async def test_specialist_and_evaluator_cannot_allocate_or_contain() -> None:
    scenario = await _owner_led_evaluated_company()
    target = {"kind": "cell", "id": CELL_ID, "revision": scenario.cell_revision}

    for actor in ("specialist", "evaluator"):
        before = await scenario.store.export_company(COMPANY_ID)
        with pytest.raises(AuthorizationError):
            await scenario.service.invoke(
                "allocation.propose",
                scenario.principals[actor],
                {
                    "idempotency_key": f"deny-{actor}-propose",
                    "expected_revision": 0,
                    "correlation_id": stable_uuid(f"deny-{actor}"),
                    "operation_intent": "allocation.propose",
                    "payload": {
                        "allocation_id": stable_uuid(f"{actor}-allocation"),
                        "action": "fund",
                        "target": target,
                        "stage": "exploitation",
                        "gate_decision_id": stable_uuid("missing-gate"),
                        "ranking": {
                            "formula": "exploitation-v1",
                            "score_numerator": 1,
                            "score_denominator": 1,
                            "input_snapshot": {
                                "expected_12m_contribution_profit_minor": 1,
                                "evidence_confidence_bps": 1,
                                "expected_repeatability_bps": 1,
                                "capital_at_risk_minor": 1,
                                "time_to_cash_days": 30,
                            },
                        },
                        "amount": {"currency": "USD", "amount_minor": 9_000},
                        "rationale_summary": "Should be denied.",
                    },
                },
            )
        with pytest.raises(AuthorizationError):
            await scenario.service.invoke(
                "cell.transition",
                scenario.principals[actor],
                {
                    "idempotency_key": f"deny-{actor}-pause",
                    "expected_revision": scenario.cell_revision,
                    "correlation_id": stable_uuid(f"deny-pause-{actor}"),
                    "operation_intent": "cell.transition",
                    "payload": {"cell_id": CELL_ID, "target_status": "paused"},
                },
            )
        after = await scenario.store.export_company(COMPANY_ID)
        assert after["domain_events"] == before["domain_events"]


@pytest.mark.asyncio
async def test_owner_cannot_kill_or_amend_constitution() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    before = await scenario.store.export_company(COMPANY_ID)

    with pytest.raises(AuthorizationError):
        await scenario.service.invoke(
            "cell.transition",
            scenario.principals["owner"],
            {
                "idempotency_key": "deny-owner-kill",
                "expected_revision": scenario.cell_revision,
                "correlation_id": stable_uuid("deny-owner-kill"),
                "operation_intent": "cell.transition",
                "payload": {"cell_id": CELL_ID, "target_status": "killed"},
            },
        )

    target = {"kind": "cell", "id": CELL_ID, "revision": scenario.cell_revision}
    with pytest.raises(AuthorizationError):
        await scenario.service.invoke(
            "gate.evaluate",
            scenario.principals["owner"],
            {
                "idempotency_key": "deny-owner-kill-gate",
                "expected_revision": 0,
                "correlation_id": stable_uuid("deny-owner-kill-gate"),
                "operation_intent": "gate.evaluate",
                "payload": {
                    "action": "kill",
                    "stage": "containment",
                    "target": target,
                    "containment_basis": {
                        "kind": "cell_owner_authorized",
                        "summary": "Owner cannot authorize irreversible kill.",
                    },
                },
            },
        )

    with pytest.raises(AuthorizationError):
        await scenario.service.invoke(
            "constitution.amend",
            scenario.principals["owner"],
            {
                "idempotency_key": "deny-owner-amend",
                "expected_revision": 1,
                "correlation_id": stable_uuid("deny-owner-amend"),
                "operation_intent": "constitution.amend",
                "payload": {
                    "budget_policy": {
                        "unit": "tries",
                        "company_limit_minor": 20,
                        "cell_limit_minor": 10,
                        "experiment_limit_minor": 4,
                        "single_action_limit_minor": 4,
                        "exploration_share_bps": 2_000,
                    }
                },
            },
        )

    after = await scenario.store.export_company(COMPANY_ID)
    assert after["domain_events"] == before["domain_events"]


@pytest.mark.asyncio
async def test_owner_cannot_evaluate_and_evaluator_cannot_execute() -> None:
    scenario = await _owner_led_evaluated_company()
    described_owner = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    described_evaluator = await scenario.service.invoke(
        "company.describe",
        scenario.principals["evaluator"],
        {},
    )
    assert "evaluation.submit" not in described_owner["principal"]["effective_capabilities"]
    assert "experiment.start" not in described_evaluator["principal"]["effective_capabilities"]
    assert "allocation.commit" not in described_evaluator["principal"]["effective_capabilities"]
    assert "evidence.append" not in described_evaluator["principal"]["effective_capabilities"]


@pytest.mark.asyncio
async def test_allocator_and_immune_remain_optional_for_the_owner_path() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    del scenario.principals["allocator"]
    del scenario.principals["immune"]
    await scenario.create_cell()
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()
    paused = await scenario.invoke(
        "cell.transition",
        "owner",
        {"cell_id": CELL_ID, "target_status": "paused"},
        expected_revision=scenario.cell_revision,
        key="owner-pause-without-deferred-roles",
    )
    assert paused["state"]["cell"]["status"] == "paused"


@pytest.mark.asyncio
async def test_owner_cannot_allocate_a_cell_it_is_not_bound_to() -> None:
    scenario = await _owner_led_evaluated_company()
    other_owner = make_principal(
        Role.CELL_OWNER,
        principal_id=stable_uuid("other-cell-owner"),
        agent_run_id=stable_uuid("other-cell-owner-run"),
    )
    scenario.principals["other_owner"] = other_owner
    other_definition_payload = deepcopy(
        __import__("tests.acceptance.support", fromlist=["load_definition"]).load_definition(
            "cell-owner"
        )
    )
    other_definition_payload.update(
        definition_id=stable_uuid("other-owner-definition"),
        name="other-cell-owner",
    )
    await scenario.invoke(
        "agent.register",
        "founder",
        {
            "principal_id": other_owner.principal_id,
            "cell_id": stable_uuid("unrelated-cell"),
            "definition": other_definition_payload,
        },
        expected_revision=0,
        key="register-other-owner",
    )

    before = await scenario.store.export_company(COMPANY_ID)
    with pytest.raises(AuthorizationError):
        await scenario.service.invoke(
            "allocation.propose",
            other_owner,
            {
                "idempotency_key": "deny-foreign-cell-allocate",
                "expected_revision": 0,
                "correlation_id": stable_uuid("deny-foreign-cell"),
                "operation_intent": "allocation.propose",
                "payload": {
                    "allocation_id": stable_uuid("foreign-allocation"),
                    "action": "maintain",
                    "target": {"kind": "cell", "id": CELL_ID, "revision": scenario.cell_revision},
                    "stage": "exploitation",
                    "gate_decision_id": stable_uuid("missing"),
                    "ranking": {
                        "formula": "exploitation-v1",
                        "score_numerator": 1,
                        "score_denominator": 1,
                        "input_snapshot": {
                            "expected_12m_contribution_profit_minor": 1,
                            "evidence_confidence_bps": 1,
                            "expected_repeatability_bps": 1,
                            "capital_at_risk_minor": 1,
                            "time_to_cash_days": 30,
                        },
                    },
                    "amount": None,
                    "rationale_summary": "Foreign cell must be denied.",
                },
            },
        )
    after = await scenario.store.export_company(COMPANY_ID)
    assert after["domain_events"] == before["domain_events"]

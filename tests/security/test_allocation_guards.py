"""Focused service guards for allocation scope, constitution, and null funding."""

from __future__ import annotations

from copy import deepcopy

import pytest

from ai_nature_company.errors import ConflictError
from tests.acceptance.scenario import Scenario
from tests.acceptance.support import (
    CELL_ID,
    COMPANY_ID,
    EVALUATION_ID,
    stable_uuid,
)


@pytest.mark.asyncio
async def test_stale_gate_constitution_cannot_be_rebound_to_a_proposal() -> None:
    scenario = Scenario()
    await scenario.build_evaluated_experiment()
    await scenario.evaluate_gates()

    replacement_id = stable_uuid("replacement-constitution")
    async with scenario.store.transaction(COMPANY_ID) as tx:
        company = await tx.get_projection("company", COMPANY_ID)
        assert company is not None
        old_id = company["state"]["active_constitution_id"]
        old_constitution = await tx.get_projection("constitution", old_id)
        assert old_constitution is not None

        replacement = deepcopy(old_constitution["state"])
        replacement.update(
            constitution_id=replacement_id,
            version=int(replacement["version"]) + 1,
            status="active",
        )
        await tx.put_projection(
            projection_type="constitution",
            projection_id=replacement_id,
            state=replacement,
            expected_revision=0,
            updated_at=scenario.clock(),
        )
        company_state = dict(company["state"])
        company_state.update(
            active_constitution_id=replacement_id,
            revision=int(company_state["revision"]) + 1,
        )
        await tx.put_projection(
            projection_type="company",
            projection_id=COMPANY_ID,
            state=company_state,
            expected_revision=company["revision"],
            updated_at=scenario.clock(),
        )

    before = await scenario.store.export_company(COMPANY_ID)
    with pytest.raises(ConflictError, match="constitution changed"):
        await scenario.propose_allocation(key="reject-stale-gate-constitution")
    after = await scenario.store.export_company(COMPANY_ID)

    assert after["domain_events"] == before["domain_events"]
    assert after["audit_events"] == before["audit_events"]
    assert not any(item["projection_type"] == "allocation" for item in after["projections"])


@pytest.mark.asyncio
async def test_non_fund_maintain_commits_with_null_amount_and_no_reservation() -> None:
    scenario = Scenario()
    await scenario.build_evaluated_experiment()
    target = {"kind": "cell", "id": CELL_ID, "revision": scenario.cell_revision}
    gate_response = await scenario.invoke(
        "gate.evaluate",
        "allocator",
        {
            "action": "maintain",
            "stage": "exploitation",
            "target": target,
            "evaluation_id": EVALUATION_ID,
            "amount": None,
        },
        expected_revision=0,
        key="evaluate-maintain",
    )
    gate = gate_response["state"]["decision"]
    allocation_id = stable_uuid("maintain-allocation")
    proposal = await scenario.invoke(
        "allocation.propose",
        "allocator",
        {
            "allocation_id": allocation_id,
            "action": "maintain",
            "target": target,
            "stage": "exploitation",
            "gate_decision_id": gate["decision_id"],
            "ranking": {
                "formula": "exploitation-v1",
                "score_numerator": 1,
                "score_denominator": 1,
                "input_snapshot": {
                    "expected_12m_contribution_profit_minor": 500_000,
                    "evidence_confidence_bps": 9_000,
                    "expected_repeatability_bps": 6_000,
                    "capital_at_risk_minor": 10_000,
                    "time_to_cash_days": 30,
                },
            },
            "amount": None,
            "rationale_summary": "Maintain the cell without reserving new capital.",
        },
        expected_revision=0,
        key="propose-maintain",
    )
    proposed = proposal["state"]["allocation"]
    before = await scenario.store.export_company(COMPANY_ID)
    committed = await scenario.invoke(
        "allocation.commit",
        "allocator",
        {"allocation_id": allocation_id},
        expected_revision=proposed["revision"],
        key="commit-maintain",
    )
    after = await scenario.store.export_company(COMPANY_ID)

    assert committed["state"]["allocation"]["amount"] is None
    assert committed["state"]["allocation"]["decision_state"] == "committed"
    assert committed["state"]["reservation"] is None
    assert after["budgets"] == before["budgets"]
    assert after["reservations"] == before["reservations"]

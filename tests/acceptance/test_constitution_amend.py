"""Founder-only constitution.amend: unit realignment and fail-closed experiment start."""

from __future__ import annotations

import pytest

from ai_nature_company.errors import AuthorizationError, ConflictError, ValidationError
from ai_nature_company.ledger_units import identity_of

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, mutation, stable_uuid

TRIES_POLICY = {
    "unit": "tries",
    "company_limit_minor": 20,
    "cell_limit_minor": 10,
    "experiment_limit_minor": 4,
    "single_action_limit_minor": 4,
    "exploration_share_bps": 2_000,
}

MONEY_POLICY = {
    "currency": "USD",
    "company_limit_minor": 100_000,
    "cell_limit_minor": 50_000,
    "experiment_limit_minor": 10_000,
    "single_action_limit_minor": 10_000,
    "exploration_share_bps": 2_000,
}


def _tries_policy(
    *,
    company: int = 20,
    cell: int = 10,
    experiment: int = 4,
) -> dict[str, int | str]:
    return {
        "unit": "tries",
        "company_limit_minor": company,
        "cell_limit_minor": cell,
        "experiment_limit_minor": experiment,
        "single_action_limit_minor": experiment,
        "exploration_share_bps": 2_000,
    }


async def _amend(
    scenario: Scenario,
    actor: str,
    budget_policy: dict[str, object],
    *,
    expected_revision: int,
    key: str,
) -> dict[str, object]:
    return await scenario.invoke(
        "constitution.amend",
        actor,
        {"budget_policy": budget_policy},
        expected_revision=expected_revision,
        key=key,
    )


async def _active_constraint_ledgers(scenario: Scenario) -> list[dict[str, object]]:
    async with scenario.store.transaction(COMPANY_ID) as tx:
        return [
            budget
            for budget in await tx.list_budgets(limit=1_000)
            if budget.get("status") == "active"
            and budget.get("scope") in {"company", "cell", "stage"}
        ]


@pytest.mark.asyncio
async def test_founder_amends_usd_to_tries_then_matching_experiment_starts() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    constitution = (
        await scenario.service.invoke("constitution.get", scenario.principals["founder"], {})
    )["constitution"]
    assert constitution["version"] == 1
    assert constitution["budget_policy"].get("unit", "money") == "money"
    assert constitution["budget_policy"]["currency"] == "USD"

    with pytest.raises(AuthorizationError):
        await scenario.service.invoke(
            "constitution.amend",
            scenario.principals["owner"],
            mutation(
                "constitution.amend",
                {"budget_policy": TRIES_POLICY},
                expected_revision=1,
                key="deny-owner-amend",
            ),
        )

    with pytest.raises(ConflictError, match="constitution version"):
        await scenario.service.invoke(
            "constitution.amend",
            scenario.principals["founder"],
            mutation(
                "constitution.amend",
                {"budget_policy": TRIES_POLICY},
                expected_revision=0,
                key="deny-stale-amend",
            ),
        )

    await scenario.register_experiment(budget_minor=10_000)
    await scenario.start_experiment()
    money_ledgers_before = await _active_constraint_ledgers(scenario)
    assert money_ledgers_before
    assert {identity_of(row) for row in money_ledgers_before} == {("money", "USD")}
    assert any(row["reserved"] > 0 for row in money_ledgers_before)

    amended = await _amend(
        scenario,
        "founder",
        TRIES_POLICY,
        expected_revision=1,
        key="amend-usd-to-tries",
    )
    state = amended["state"]["constitution"]
    assert state["constitution_id"] == constitution["constitution_id"]
    assert state["version"] == 2
    assert state["status"] == "active"
    assert state["budget_policy"]["unit"] == "tries"
    assert "currency" not in state["budget_policy"]
    assert state["fitness_policy"]["rule_version"] == "fitness-v1"
    assert "constitution.activated" in {
        event["event_type"]
        for event in (await scenario.store.export_company(COMPANY_ID))["domain_events"]
    }

    async with scenario.store.transaction(COMPANY_ID) as tx:
        all_ledgers = await tx.list_budgets(limit=1_000)
    constraint = [row for row in all_ledgers if row.get("scope") in {"company", "cell", "stage"}]
    active = [row for row in constraint if row["status"] == "active"]
    closed = [row for row in constraint if row["status"] == "closed"]
    assert {identity_of(row) for row in active} == {("tries", None)}
    assert {identity_of(row) for row in closed} == {("money", "USD")}
    assert all(row["reserved"] == 0 for row in active)
    assert any(row["reserved"] > 0 for row in closed)
    assert not any(identity_of(row) == ("tries", None) and row["currency"] for row in constraint)

    cell_view = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    cell = cell_view["cell"]
    async with scenario.store.transaction(COMPANY_ID) as tx:
        cell_budget = await tx.get_budget(str(cell["budget_id"]))
    assert cell_budget is not None
    assert identity_of(cell_budget) == ("tries", None)
    assert cell_budget["status"] == "active"

    matching_id = stable_uuid("experiment-tries-match")
    await scenario.register_experiment(
        experiment_id=matching_id,
        budget={"unit": "tries", "amount_minor": 4},
        key="register-tries-after-amend",
    )
    started = await scenario.start_experiment(
        experiment_id=matching_id,
        key="start-tries-after-amend",
    )
    reservation = started["state"]["reservation"]
    assert reservation["unit"] == "tries"
    assert reservation["currency"] is None

    with pytest.raises(ValidationError, match="unit must match"):
        await scenario.register_experiment(
            experiment_id=stable_uuid("experiment-money-mismatch"),
            budget={"currency": "USD", "amount_minor": 1_000},
            key="register-money-after-tries-constitution",
        )

    next_amend = await _amend(
        scenario,
        "founder",
        _tries_policy(company=30, cell=12, experiment=5),
        expected_revision=2,
        key="amend-tries-v3",
    )
    assert next_amend["state"]["constitution"]["version"] == 3
    assert next_amend["state"]["constitution"]["budget_policy"]["company_limit_minor"] == 30
    assert next_amend["state"]["constitution"]["budget_policy"]["unit"] == "tries"

    later_id = stable_uuid("experiment-tries-still-registered")
    await scenario.register_experiment(
        experiment_id=later_id,
        budget={"unit": "tries", "amount_minor": 5},
        key="register-tries-before-usd-amend",
    )
    await _amend(
        scenario,
        "founder",
        MONEY_POLICY,
        expected_revision=3,
        key="amend-back-to-usd",
    )
    with pytest.raises(ValidationError, match="unit must match"):
        await scenario.start_experiment(
            experiment_id=later_id,
            key="start-tries-after-usd-constitution",
        )


@pytest.mark.asyncio
async def test_protocol_describe_exposes_constitution_amend() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["founder"],
        {},
    )
    assert "constitution.amend" in described["tools"]
    assert described["inference_inside_substrate"] is False
    assert described["agent_scheduler_inside_substrate"] is False

"""Acceptance coverage for time, tries, cycles, and fail-closed unit mixing."""

from __future__ import annotations

import pytest

from ai_nature_company.errors import BudgetError, ValidationError

from .scenario import Scenario
from .support import EXPERIMENT_ID


async def _ready_cell(
    scenario: Scenario,
    *,
    budget_limit: str | int,
    unit: str | None = None,
    currency: str | None = "USD",
) -> None:
    await scenario.create_cell(budget_limit=budget_limit, unit=unit, currency=currency)
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unit", "company_limit", "cell_limit", "experiment_limit", "spent"),
    [
        ("time", 36_000, 18_000, 3_600, 900),
        ("tries", 20, 10, 4, 2),
        ("cycles", 80, 40, 16, 5),
    ],
)
async def test_experiment_reserve_start_close_for_non_money_units(
    unit: str,
    company_limit: int,
    cell_limit: int,
    experiment_limit: int,
    spent: int,
) -> None:
    scenario = Scenario()
    await scenario.bootstrap(
        constitution_unit=unit,
        constitution_currency=None,
        company_limit_minor=company_limit,
        cell_limit_minor=cell_limit,
        experiment_limit_minor=experiment_limit,
        single_action_limit_minor=experiment_limit,
        budget_unit=unit,
        budget_currency=None,
        budget_limit=company_limit,
    )
    constitution = scenario.responses["bootstrap"]["state"]["constitution"]
    assert constitution["budget_policy"]["unit"] == unit
    assert "currency" not in constitution["budget_policy"]
    await _ready_cell(scenario, budget_limit=cell_limit, unit=unit, currency=None)
    await scenario.register_experiment(budget={"unit": unit, "amount_minor": experiment_limit})
    started = await scenario.start_experiment()
    reservation = started["state"]["reservation"]
    assert reservation["unit"] == unit
    assert reservation["currency"] is None
    assert int(reservation["amount"]) == experiment_limit
    work = await scenario.create_work(
        budget={"unit": unit, "maximum_cost_minor": min(spent, experiment_limit)}
    )
    assert work["state"]["work_item"]["budget"] == {
        "unit": unit,
        "maximum_cost_minor": min(spent, experiment_limit),
    }
    closed = await scenario.close_named_experiment(
        EXPERIMENT_ID,
        actual_cost={"unit": unit, "amount_minor": spent},
    )
    assert closed["state"]["experiment"]["actual_cost"] == {
        "unit": unit,
        "amount_minor": spent,
    }
    assert closed["state"]["experiment"]["state"] == "evaluating"


@pytest.mark.asyncio
async def test_mixing_units_is_rejected_and_overspend_is_budget_exceeded() -> None:
    usd = Scenario()
    await usd.bootstrap()
    await _ready_cell(usd, budget_limit="500.00")
    with pytest.raises(ValidationError, match="unit must match"):
        await usd.register_experiment(budget={"unit": "tries", "amount_minor": 3})
    with pytest.raises(ValidationError, match="unit must match"):
        await usd.create_work(
            experiment_id=None,
            budget={"unit": "time", "maximum_cost_minor": 60},
        )

    time = Scenario()
    await time.bootstrap(
        constitution_unit="time",
        constitution_currency=None,
        company_limit_minor=3_600,
        cell_limit_minor=1_800,
        experiment_limit_minor=1_200,
        single_action_limit_minor=1_200,
        budget_unit="time",
        budget_currency=None,
        budget_limit=3_600,
    )
    with pytest.raises(ValidationError, match="unit must match"):
        await time.create_cell(budget_limit="10.00", currency="USD")
    await _ready_cell(time, budget_limit=1_800, unit="time", currency=None)
    await time.register_experiment(
        budget={"unit": "time", "amount_minor": 1_200},
        key="register-time-overspend",
    )
    with pytest.raises(BudgetError, match="available budget"):
        await time.start_experiment(key="start-time-overspend")


@pytest.mark.asyncio
async def test_usd_money_path_still_bootstraps_without_explicit_unit() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    policy = scenario.responses["bootstrap"]["state"]["constitution"]["budget_policy"]
    assert policy["currency"] == "USD"
    assert policy.get("unit", "money") == "money"
    await scenario.create_cell()
    budget = scenario.responses["cell.create"]["state"]["budget"]
    assert budget["currency"] == "USD"
    assert budget.get("unit", "money") == "money"

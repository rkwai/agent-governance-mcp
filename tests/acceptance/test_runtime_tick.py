"""Deterministic runtime tick: lease expiry, due-experiment closeout, no model."""

from __future__ import annotations

import pytest

from ai_nature_company.experiment_stop import WINDOW_ELAPSED

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, EXPERIMENT_ID, WORK_ITEM_ID


@pytest.mark.asyncio
async def test_tick_closes_a_due_experiment_and_makes_evaluation_claimable() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    before = await scenario.store.export_company(COMPANY_ID)
    scenario.clock.advance(days=8)

    result = await scenario.service.tick(COMPANY_ID)

    assert result["inference_inside_substrate"] is False
    assert result["closed_experiment_ids"] == [EXPERIMENT_ID]
    assert result["closeout_work_item_ids"]
    experiment = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    bets = experiment["experiments"]
    due = next(item for item in bets if item["experiment_id"] == EXPERIMENT_ID)
    assert due["state"] == "evaluating"
    assert due["stop_state"] == "fired"
    assert due["close_eligible"] is False
    assert WINDOW_ELAPSED in {rule["rule_id"] for rule in due["fired_rules"]}

    claimed = await scenario.invoke(
        "work.claim",
        "evaluator",
        {
            "work_item_id": result["closeout_work_item_ids"][0],
            "lease_seconds": 300,
        },
        expected_revision=1,
        key="evaluator-claims-tick-closeout",
    )
    work = claimed["state"]["work_item"]
    assert work["kind"] == "evaluate_experiment"
    assert "evaluation.submit" in work["eligible_capabilities"]
    assert work["status"] == "claimed"

    after = await scenario.store.export_company(COMPANY_ID)
    assert len(after["domain_events"]) > len(before["domain_events"])
    assert not any(
        "openai" in str(event).lower() or "model" in event.get("event_type", "")
        for event in after["domain_events"]
    )


@pytest.mark.asyncio
async def test_tick_is_idempotent_and_does_not_reclose() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    scenario.clock.advance(days=8)
    first = await scenario.service.tick(COMPANY_ID)
    snapshot = await scenario.store.export_company(COMPANY_ID)
    second = await scenario.service.tick(COMPANY_ID)

    assert first["closed_experiment_ids"] == [EXPERIMENT_ID]
    assert second["closed_experiment_ids"] == []
    assert second["closeout_work_item_ids"] == []
    after = await scenario.store.export_company(COMPANY_ID)
    assert after["domain_events"] == snapshot["domain_events"]


@pytest.mark.asyncio
async def test_tick_expires_a_due_lease_and_returns_work_to_open() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    claimed = await scenario.claim_work()
    assert claimed["state"]["work_item"]["status"] == "claimed"
    scenario.clock.advance(seconds=400)

    result = await scenario.service.tick(COMPANY_ID)

    assert WORK_ITEM_ID in result["expired_work_item_ids"]
    viewed = await scenario.store.export_company(COMPANY_ID)
    work = next(
        item["state"]
        for item in viewed["projections"]
        if item["projection_type"] == "work_item" and item["projection_id"] == WORK_ITEM_ID
    )
    assert work["status"] == "open"
    lease = next(item for item in viewed["work_leases"] if item["work_item_id"] == WORK_ITEM_ID)
    assert lease["status"] != "active"


@pytest.mark.asyncio
async def test_tick_does_not_close_an_in_window_experiment() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()

    result = await scenario.service.tick(COMPANY_ID)

    assert result["closed_experiment_ids"] == []
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    due = next(item for item in viewed["experiments"] if item["experiment_id"] == EXPERIMENT_ID)
    assert due["state"] == "running"
    assert due["stop_state"] == "clear"
    assert due["close_eligible"] is False


@pytest.mark.asyncio
async def test_company_describe_shows_remaining_budget_and_stop_state() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    portfolio = described["portfolio"]
    assert portfolio["remaining_budget_by_unit"]
    cell = next(item for item in portfolio["cells"] if item["cell_id"] == CELL_ID)
    assert "remaining_budget_minor" in cell
    assert cell["eligibility"]["kill_requires_founder"] is True
    experiment = next(
        item for item in cell["experiments"] if item["experiment_id"] == EXPERIMENT_ID
    )
    assert experiment["stop_state"] == "clear"
    assert "packets_sent" not in described
    assert "packets_sent" not in portfolio

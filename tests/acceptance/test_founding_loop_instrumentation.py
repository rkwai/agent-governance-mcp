"""Founding-loop instrumentation: try-spend, evidence class, stale work."""

from __future__ import annotations

from datetime import timedelta

import pytest

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, EVIDENCE_ID, WORK_ITEM_ID, stable_uuid

FAIL_WORK_ID = stable_uuid("fail-try-work")
OVERDUE_WORK_ID = stable_uuid("overdue-open-work")
ACTIVITY_EVIDENCE_ID = stable_uuid("activity-packet")
FITNESS_EVIDENCE_ID = stable_uuid("fitness-outcome")


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


@pytest.mark.asyncio
async def test_work_complete_and_fail_spend_tries_against_the_reservation() -> None:
    scenario = await _tries_running_experiment()
    before = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert before["ledger"]["spent_minor"] == 0
    assert before["ledger"]["reserved_minor"] == 4
    assert before["cell"]["fitness_evidence_count"] == 0

    await scenario.create_work(budget={"unit": "tries", "maximum_cost_minor": 1})
    await scenario.claim_work()
    await scenario.append_evidence(
        evidence_type="other",
        observation={
            "value": 1,
            "unit": "packet_sent",
            "summary": "Scout sent one first-touch packet.",
        },
    )
    await scenario.complete_work(actual_cost={"unit": "tries", "amount_minor": 1})

    after_complete = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_complete["ledger"]["spent_minor"] == 1
    assert after_complete["ledger"]["reserved_minor"] == 3
    assert after_complete["ledger"]["available_minor"] == before["ledger"]["available_minor"]
    assert after_complete["cell"]["spent_minor"] == 1
    assert after_complete["cell"]["activity_evidence_count"] == 1
    assert after_complete["cell"]["fitness_evidence_count"] == 0

    await scenario.create_work(
        work_item_id=FAIL_WORK_ID,
        budget={"unit": "tries", "maximum_cost_minor": 2},
        key="create-fail-work",
    )
    await scenario.claim_work(work_item_id=FAIL_WORK_ID, key="claim-fail-work")
    await scenario.fail_work(
        work_item_id=FAIL_WORK_ID,
        actual_cost={"unit": "tries", "amount_minor": 2},
        key="fail-try-work",
    )

    after_fail = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_fail["ledger"]["spent_minor"] == 3
    assert after_fail["ledger"]["reserved_minor"] == 1
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    assert viewed["spent_minor"] == 3
    assert viewed["fitness_evidence_count"] == 0


@pytest.mark.asyncio
async def test_cell_projection_counts_activity_fitness_and_stale_open_work() -> None:
    scenario = await _tries_running_experiment()
    await scenario.create_work(budget={"unit": "tries", "maximum_cost_minor": 1})
    await scenario.claim_work()
    await scenario.append_evidence(
        evidence_id=ACTIVITY_EVIDENCE_ID,
        evidence_type="other",
        observation={
            "value": 3,
            "unit": "scout_ranked_drafts",
            "summary": "Scout ranked three draft packets.",
        },
        key="append-activity",
    )
    await scenario.complete_work(
        evidence_id=ACTIVITY_EVIDENCE_ID,
        actual_cost={"unit": "tries", "amount_minor": 1},
    )

    fitness_work = stable_uuid("fitness-work")
    await scenario.create_work(
        work_item_id=fitness_work,
        budget={"unit": "tries", "maximum_cost_minor": 1},
        key="create-fitness-work",
    )
    await scenario.claim_work(work_item_id=fitness_work, key="claim-fitness-work")
    await scenario.append_evidence(
        evidence_id=FITNESS_EVIDENCE_ID,
        evidence_type="outcome",
        observation={
            "value": 9_000,
            "unit": "basis_points",
            "summary": "A verified customer outcome met the frozen threshold.",
        },
        key="append-fitness",
    )
    await scenario.complete_work(
        work_item_id=fitness_work,
        evidence_id=FITNESS_EVIDENCE_ID,
        actual_cost={"unit": "tries", "amount_minor": 1},
        key="complete-fitness-work",
    )

    deadline = (scenario.clock() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await scenario.create_work(
        work_item_id=OVERDUE_WORK_ID,
        budget={"unit": "tries", "maximum_cost_minor": 1},
        deadline_at=deadline,
        key="create-overdue-work",
    )
    scenario.clock.advance(hours=2)

    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    described = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert viewed["activity_evidence_count"] == 1
    assert viewed["fitness_evidence_count"] == 1
    assert viewed["stale_open_work_count"] == 1
    assert viewed["spent_minor"] == 2
    assert described["cell"]["activity_evidence_count"] == 1
    assert described["cell"]["fitness_evidence_count"] == 1
    assert described["cell"]["stale_open_work_count"] == 1
    assert described["cell"]["spent_minor"] == 2


@pytest.mark.asyncio
async def test_tick_fails_overdue_open_work_and_spends_its_try_budget() -> None:
    scenario = await _tries_running_experiment()
    deadline = (scenario.clock() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await scenario.create_work(
        work_item_id=OVERDUE_WORK_ID,
        budget={"unit": "tries", "maximum_cost_minor": 1},
        deadline_at=deadline,
        key="create-overdue-work",
    )
    before = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert before["cell"]["stale_open_work_count"] == 0
    scenario.clock.advance(hours=2)

    result = await scenario.service.tick(COMPANY_ID)

    assert OVERDUE_WORK_ID in result["failed_overdue_work_item_ids"]
    exported = await scenario.store.export_company(COMPANY_ID)
    work = next(
        item["state"]
        for item in exported["projections"]
        if item["projection_type"] == "work_item" and item["projection_id"] == OVERDUE_WORK_ID
    )
    assert work["status"] == "failed"
    runtime = next(
        item["state"]
        for item in exported["projections"]
        if item["projection_type"] == "work_runtime" and item["projection_id"] == OVERDUE_WORK_ID
    )
    assert runtime["failure_code"] == "deadline_elapsed"
    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["ledger"]["spent_minor"] == 1
    assert after["ledger"]["reserved_minor"] == 3
    assert after["cell"]["stale_open_work_count"] == 0
    replay = await scenario.service.tick(COMPANY_ID)
    assert replay["failed_overdue_work_item_ids"] == []


@pytest.mark.asyncio
async def test_money_work_complete_still_spends_the_existing_usd_reservation() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    await scenario.claim_work()
    await scenario.append_evidence()
    await scenario.complete_work()
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    assert viewed["spent_minor"] == 500
    assert viewed["fitness_evidence_count"] == 1
    assert WORK_ITEM_ID
    assert EVIDENCE_ID

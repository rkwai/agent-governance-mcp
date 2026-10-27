"""On-demand maintenance scan: findings and "would do", zero state mutations."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from ai_nature_company.errors import ValidationError
from ai_nature_company.experiment_stop import WINDOW_ELAPSED
from ai_nature_company.maintenance import (
    MaintenanceAgentRun,
    MaintenanceAgentRunScan,
    MaintenanceMutationRefused,
    ReadOnlyTransaction,
    filter_report_for_cell,
    scan_projections,
)

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, EXPERIMENT_ID, WORK_ITEM_ID, stable_uuid

UNCLAIMED_WORK_ID = stable_uuid("work-item-unclaimed-render")
EXPIRING_RUN_ID = stable_uuid("run-expiring")
EXPIRED_RUN_ID = stable_uuid("run-expired")


async def _october_fourth_shape(scenario: Scenario) -> list[MaintenanceAgentRun]:
    """Replay the 10/4 sit-the-seat shape on acceptance data.

    * the experiment ran past its window with no evaluation recorded,
    * Render's item expired unclaimed (open work past its deadline),
    * a claimed item's lease expired,
    * agent runs were 72 hours from expiry (and one had already expired).
    """

    await scenario.build_running_experiment()
    await scenario.create_work()
    await scenario.claim_work(lease_seconds=300)
    deadline = (scenario.clock() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    await scenario.create_work(
        work_item_id=UNCLAIMED_WORK_ID,
        key="create-unclaimed-render-work",
        deadline_at=deadline,
    )
    scenario.clock.advance(days=8)
    now = scenario.clock()
    return [
        MaintenanceAgentRun(
            agent_run_id=EXPIRING_RUN_ID,
            agent_instance_id=stable_uuid("instance-expiring"),
            role="cell_owner",
            cell_id=CELL_ID,
            expires_at=now + timedelta(hours=48),
        ),
        MaintenanceAgentRun(
            agent_run_id=EXPIRED_RUN_ID,
            agent_instance_id=stable_uuid("instance-expired"),
            role="specialist",
            cell_id=CELL_ID,
            expires_at=now - timedelta(hours=1),
        ),
    ]


def _kinds(report: dict[str, Any]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for item in report["findings"]:
        found.setdefault(item["kind"], set()).add(item["resource_id"])
    return found


@pytest.mark.asyncio
async def test_report_mode_makes_zero_state_mutations() -> None:
    scenario = Scenario()
    runs = await _october_fourth_shape(scenario)
    before = await scenario.store.export_company(COMPANY_ID)

    report = await scenario.service.maintenance_scan(COMPANY_ID, agent_runs=runs)

    after = await scenario.store.export_company(COMPANY_ID)
    assert report["mode"] == "on_demand"
    assert report["state_mutations"] == 0
    assert report["findings"], "fixture must produce findings"
    # The scan writes nothing at all: no report projection and no audit row.
    after.pop("exported_at", None)
    before.pop("exported_at", None)
    assert after == before


@pytest.mark.asyncio
async def test_replayed_october_fourth_fixture_is_flagged() -> None:
    scenario = Scenario()
    runs = await _october_fourth_shape(scenario)

    report = await scenario.service.maintenance_scan(COMPANY_ID, agent_runs=runs)

    kinds = _kinds(report)
    assert kinds["agent_run_expiring"] == {EXPIRING_RUN_ID}
    assert kinds["agent_run_expired"] == {EXPIRED_RUN_ID}
    assert kinds["open_work_deadline_elapsed"] == {UNCLAIMED_WORK_ID}
    assert kinds["work_lease_expired"] == {WORK_ITEM_ID}
    assert kinds["experiment_close_eligible"] == {EXPERIMENT_ID}
    experiment = next(
        item for item in report["findings"] if item["kind"] == "experiment_close_eligible"
    )
    assert experiment["observed"]["has_evaluation"] is False
    assert WINDOW_ELAPSED in experiment["observed"]["fired_rules"]
    actions = {(item["action"], item["resource_id"]) for item in report["would_do"]}
    assert ("close_experiment_and_open_closeout_evaluation_work", EXPERIMENT_ID) in actions
    assert ("fail_work_deadline_elapsed", UNCLAIMED_WORK_ID) in actions
    assert ("expire_lease_and_fail_work_deadline_elapsed", WORK_ITEM_ID) in actions
    assert ("rotate_agent_run", EXPIRING_RUN_ID) in actions
    assert report["sections"]["founder_signing_paths"]["status"] == "todo"
    assert report["counts"]["experiment_close_eligible"] == 1

    # Nothing moved: the experiment still runs and the work is still claimed/open.
    cell = await scenario.service.invoke(
        "cell.get", scenario.principals["owner"], {"cell_id": CELL_ID}
    )
    due = next(item for item in cell["experiments"] if item["experiment_id"] == EXPERIMENT_ID)
    assert due["state"] == "running"

    # The mutating tick (still manual-only) would act on exactly what was flagged.
    applied = await scenario.service.tick(COMPANY_ID)
    assert applied["closed_experiment_ids"] == [EXPERIMENT_ID]
    assert applied["expired_work_item_ids"] == [WORK_ITEM_ID]
    assert sorted(applied["failed_overdue_work_item_ids"]) == sorted(
        [UNCLAIMED_WORK_ID, WORK_ITEM_ID]
    )


@pytest.mark.asyncio
async def test_evaluating_experiment_without_evaluation_is_flagged() -> None:
    scenario = Scenario()
    await scenario.build_evaluating_experiment()

    report = await scenario.service.maintenance_scan(COMPANY_ID)

    finding = next(
        item for item in report["findings"] if item["kind"] == "experiment_awaiting_evaluation"
    )
    assert finding["resource_id"] == EXPERIMENT_ID
    assert finding["would_do"] == "open_closeout_evaluation_work"
    assert finding["observed"]["closeout_work_status"] is None
    # Runs are read from the store in the same transaction (none seeded here).
    assert report["sections"]["agent_runs"]["status"] == "complete"
    assert report["scanned"]["agent_runs"] == 0


@pytest.mark.asyncio
async def test_active_cell_with_no_open_work_or_experiments_is_flagged() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()

    report = await scenario.service.maintenance_scan(COMPANY_ID)

    assert _kinds(report).get("active_cell_idle") == {CELL_ID}


@pytest.mark.asyncio
async def test_run_on_killed_cell_and_quiet_company_reports() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    report = await scenario.service.maintenance_scan(
        COMPANY_ID,
        agent_runs=[
            MaintenanceAgentRun(
                agent_run_id=EXPIRING_RUN_ID,
                agent_instance_id=stable_uuid("instance-far"),
                role="cell_owner",
                cell_id=CELL_ID,
                expires_at=scenario.clock() + timedelta(days=13),
            )
        ],
    )
    assert report["findings"] == []
    assert report["would_do"] == []
    assert report["scanned"]["agent_runs"] == 1


@pytest.mark.asyncio
async def test_blank_company_and_writes_are_refused() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    with pytest.raises(ValidationError):
        await scenario.service.maintenance_scan("  ")

    async with scenario.store.transaction(COMPANY_ID) as tx:
        readonly = ReadOnlyTransaction(tx)
        assert await readonly.get_projection("company", COMPANY_ID) is not None
        for method in ("put_projection", "append_event", "expire_lease", "update_budget"):
            with pytest.raises(MaintenanceMutationRefused):
                getattr(readonly, method)


def test_cell_scoped_view_filters_findings() -> None:
    report = {
        "findings": [
            {"kind": "a", "cell_id": "c1", "resource_id": "x", "would_do": "w"},
            {"kind": "b", "cell_id": "c2", "resource_id": "y", "would_do": None},
        ],
        "would_do": [{"action": "w", "cell_id": "c1", "resource_id": "x"}],
        "counts": {"a": 1, "b": 1},
    }
    scoped = filter_report_for_cell(report, "c1")
    assert scoped["counts"] == {"a": 1}
    assert [item["resource_id"] for item in scoped["findings"]] == ["x"]
    assert scoped["scoped_to_cell_id"] == "c1"


@pytest.mark.asyncio
async def test_expired_lease_inside_deadline_would_requeue() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    await scenario.claim_work(lease_seconds=300)
    scenario.clock.advance(seconds=400)

    report = await scenario.service.maintenance_scan(COMPANY_ID)

    finding = next(item for item in report["findings"] if item["kind"] == "work_lease_expired")
    assert finding["would_do"] == "expire_lease_and_requeue_work"
    assert finding["observed"]["attempt"] == 1


async def _seed_overdue_open_work(scenario: Scenario, count: int) -> list[str]:
    """Insert synthetic overdue open work directly; the scan, not creation, is under test."""

    deadline = (scenario.clock() - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    ids = [stable_uuid(f"bulk-work-{index}") for index in range(count)]
    async with scenario.store.transaction(COMPANY_ID) as tx:
        for work_id in ids:
            await tx.put_projection(
                projection_type="work_item",
                projection_id=work_id,
                state={
                    "work_item_id": work_id,
                    "cell_id": CELL_ID,
                    "status": "open",
                    "kind": "collect_verified_sample",
                    "deadline_at": deadline,
                    "max_attempts": 3,
                    "revision": 1,
                },
                expected_revision=0,
            )
    return ids


@pytest.mark.asyncio
async def test_work_scan_pages_past_the_old_1000_limit() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    ids = await _seed_overdue_open_work(scenario, 1_250)

    report = await scenario.service.maintenance_scan(COMPANY_ID)

    flagged = {
        item["resource_id"]
        for item in report["findings"]
        if item["kind"] == "open_work_deadline_elapsed"
    }
    assert report["counts"]["open_work_deadline_elapsed"] == 1_250
    assert report["total_findings"] == 1_250
    assert report["scanned"]["work_items"] == 1_250
    assert report["sections"]["work_leases"] == {
        "status": "complete",
        "truncated": False,
        "scanned": 1_250,
        "cap": 50_000,
    }
    # The findings list is bounded, and says so with full counts.
    assert len(flagged) == 200
    assert flagged <= set(ids)
    assert report["findings_truncated"] is True
    assert report["sections"]["findings"]["status"] == "truncated"
    assert report["sections"]["findings"]["scanned"] == 1_250
    assert report["counts_by_cell"][CELL_ID]["open_work_deadline_elapsed"] == 1_250
    scoped = filter_report_for_cell(report, CELL_ID)
    assert scoped["counts"]["open_work_deadline_elapsed"] == 1_250
    assert report["truncated"] is True


@pytest.mark.asyncio
async def test_hitting_the_scan_cap_marks_sections_truncated_not_complete() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    await _seed_overdue_open_work(scenario, 30)

    report = await scenario.service.maintenance_scan(COMPANY_ID, scan_cap=25)

    work = report["sections"]["work_leases"]
    assert work == {"status": "truncated", "truncated": True, "scanned": 25, "cap": 25}
    assert report["counts"]["open_work_deadline_elapsed"] == 25
    # Idleness cannot be proven from a truncated work scan.
    assert report["sections"]["idle_cells"]["truncated"] is True
    assert "active_cell_idle" not in report["counts"]
    assert report["truncated"] is True


@pytest.mark.asyncio
async def test_exact_cap_is_complete_and_runs_scan_truncation_is_reported() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    await _seed_overdue_open_work(scenario, 25)

    report = await scenario.service.maintenance_scan(
        COMPANY_ID,
        scan_cap=25,
        agent_runs=MaintenanceAgentRunScan(runs=(), truncated=True, cap=10_000),
    )

    assert report["sections"]["work_leases"]["status"] == "complete"
    assert report["sections"]["agent_runs"] == {
        "status": "truncated",
        "truncated": True,
        "scanned": 0,
        "cap": 10_000,
    }
    assert report["truncated"] is True


@pytest.mark.asyncio
async def test_scan_projections_pages_with_small_pages_and_rejects_bad_caps() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    ids = await _seed_overdue_open_work(scenario, 23)
    async with scenario.store.transaction(COMPANY_ID) as tx:
        scan = await scan_projections(ReadOnlyTransaction(tx), "work_item", page_size=5)
        assert [record["projection_id"] for record in scan.records] == sorted(ids)
        assert scan.truncated is False
        with pytest.raises(ValidationError):
            await scan_projections(tx, "work_item", cap=0)


async def _seed_cells(scenario: Scenario, count: int, *, status: str = "active") -> list[str]:
    ids = [stable_uuid(f"bulk-cell-{index}") for index in range(count)]
    async with scenario.store.transaction(COMPANY_ID) as tx:
        for cell_id in ids:
            await tx.put_projection(
                projection_type="cell",
                projection_id=cell_id,
                state={"cell_id": cell_id, "status": status, "revision": 1},
                expected_revision=0,
            )
    return ids


@pytest.mark.asyncio
async def test_truncated_cell_scan_marks_agent_runs_truncated() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    await _seed_cells(scenario, 5, status="killed")
    runs = MaintenanceAgentRunScan(runs=(), truncated=False, cap=10_000)

    capped = await scenario.service.maintenance_scan(COMPANY_ID, scan_cap=3, agent_runs=runs)
    assert capped["sections"]["idle_cells"]["truncated"] is True
    # Runs on unscanned (possibly killed) cells could be missed.
    assert capped["sections"]["agent_runs"]["status"] == "truncated"
    assert capped["sections"]["agent_runs"]["truncated"] is True

    scenario.clock.advance(days=1)
    full = await scenario.service.maintenance_scan(COMPANY_ID, agent_runs=runs)
    assert full["sections"]["agent_runs"]["status"] == "complete"


@pytest.mark.asyncio
async def test_cell_scoped_view_hides_company_wide_totals_and_scan_counts() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    await _seed_overdue_open_work(scenario, 3)
    other_cells = await _seed_cells(scenario, 4)
    other_work = stable_uuid("other-cell-work")
    deadline = (scenario.clock() - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    async with scenario.store.transaction(COMPANY_ID) as tx:
        for index in range(7):
            work_id = stable_uuid(f"{other_work}-{index}")
            await tx.put_projection(
                projection_type="work_item",
                projection_id=work_id,
                state={
                    "work_item_id": work_id,
                    "cell_id": other_cells[0],
                    "status": "open",
                    "kind": "collect_verified_sample",
                    "deadline_at": deadline,
                    "max_attempts": 3,
                    "revision": 1,
                },
                expected_revision=0,
            )
    runs = MaintenanceAgentRunScan(
        runs=tuple(
            MaintenanceAgentRun(
                agent_run_id=stable_uuid(f"r-{index}"),
                agent_instance_id=stable_uuid(f"i-{index}"),
                role="specialist",
                cell_id=other_cells[1],
                expires_at=scenario.clock() + timedelta(days=30),
            )
            for index in range(2)
        ),
    )
    report = await scenario.service.maintenance_scan(COMPANY_ID, agent_runs=runs)
    assert report["scanned"]["work_items"] == 10
    assert report["total_findings"] > 3

    scoped = filter_report_for_cell(report, CELL_ID)
    own_total = sum(scoped["counts"].values())
    assert scoped["total_findings"] == own_total == len(scoped["findings"])
    assert scoped["findings_truncated"] is False
    assert scoped["scanned"] == {
        "cells": 1,
        "work_items": 3,
        "experiments": 0,
        "evidence": 0,
        "agent_runs": 0,
    }
    assert "scanned_by_cell" not in scoped
    assert set(scoped["counts_by_cell"]) == {CELL_ID}
    sections = scoped["sections"]
    assert sections["work_leases"]["scanned"] == 3
    assert sections["idle_cells"]["scanned"] == 1
    assert sections["agent_runs"]["scanned"] == 0
    assert sections["experiments"]["scanned"] == 0
    assert sections["findings"]["scanned"] == own_total
    assert sections["founder_signing_paths"]["scanned"] is None
    # An unrelated cell sees zeros, not the company totals.
    empty = filter_report_for_cell(report, stable_uuid("no-such-cell"))
    assert empty["total_findings"] == 0
    assert set(empty["scanned"].values()) == {0}


@pytest.mark.asyncio
async def test_cell_scoped_view_counts_real_evidence_and_report_is_v2() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    await scenario.claim_work()
    await scenario.append_evidence()

    report = await scenario.service.maintenance_scan(COMPANY_ID)

    assert report["report_version"] == 2
    assert report["scanned"]["evidence"] >= 1
    assert report["scanned_by_cell"][CELL_ID]["evidence"] == report["scanned"]["evidence"]
    scoped = filter_report_for_cell(report, CELL_ID)
    assert scoped["scanned"]["evidence"] == report["scanned"]["evidence"]
    assert scoped["scanned"]["experiments"] == 1
    other = filter_report_for_cell(report, stable_uuid("other-cell"))
    assert other["scanned"]["evidence"] == 0


def test_cell_scoped_view_tolerates_v1_reports_without_breakdown() -> None:
    v1 = {
        "report_version": 1,
        "findings": [{"kind": "a", "cell_id": "c1", "resource_id": "x", "would_do": None}],
        "would_do": [],
        "counts": {"a": 1},
        "counts_by_cell": {"c1": {"a": 1}, "c2": {"b": 9}},
        "total_findings": 10,
        "scanned": {"cells": 40, "work_items": 900, "agent_runs": None},
        "sections": {
            "work_leases": {"status": "complete", "truncated": False, "scanned": 900, "cap": 1},
            "findings": {"status": "complete", "truncated": False, "scanned": 10, "cap": 200},
        },
    }
    scoped = filter_report_for_cell(v1, "c1")
    assert scoped["report_version"] == 1
    assert scoped["total_findings"] == 1
    # No company totals and no false zeros: counts are withheld.
    assert scoped["scanned"] == {"cells": None, "work_items": None, "agent_runs": None}
    assert scoped["sections"]["work_leases"]["scanned"] is None
    assert scoped["sections"]["findings"]["scanned"] == 1

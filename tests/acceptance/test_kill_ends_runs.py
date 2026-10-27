"""Killing a cell ends its running agent runs in the same transaction."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ai_nature_company.errors import AuthorizationError, ValidationError

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, mutation, stable_uuid
from .test_cell_owner_kill import _kill, _paused_cell

OTHER_CELL_ID = stable_uuid("other-cell")


def _seed_runs(scenario: Scenario) -> tuple[list[str], str]:
    now = scenario.clock()
    own = []
    for index, role in enumerate(("cell_owner", "specialist", "evaluator")):
        run_id = stable_uuid(f"kill-run-{index}")
        scenario.store.seed_agent_run(
            COMPANY_ID,
            agent_run_id=run_id,
            agent_instance_id=stable_uuid(f"kill-instance-{index}"),
            role=role,
            cell_id=CELL_ID,
            started_at=now - timedelta(hours=1),
            expires_at=now + timedelta(days=10),
        )
        own.append(run_id)
    other = stable_uuid("other-cell-run")
    scenario.store.seed_agent_run(
        COMPANY_ID,
        agent_run_id=other,
        agent_instance_id=stable_uuid("other-instance"),
        role="specialist",
        cell_id=OTHER_CELL_ID,
        started_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=10),
    )
    return sorted(own), other


async def _kill_audit(scenario: Scenario) -> dict[str, object]:
    export = await scenario.store.export_company(COMPANY_ID)
    return next(
        item
        for item in reversed(export["audit_events"])
        if item["action"] == "cell.transition" and item["details"].get("kill_authority")
    )["details"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "founder"])
async def test_kill_ends_only_this_cells_running_runs_and_audits_them(actor: str) -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)
    own, other = _seed_runs(scenario)

    killed = await _kill(scenario, actor, revision)

    assert killed["state"]["ended_agent_run_ids"] == own
    for run_id in own:
        run = scenario.store.agent_run(COMPANY_ID, run_id)
        assert run["status"] == "revoked"
        assert run["termination_code"] == "cell_killed"
        assert run["ended_at"] == scenario.clock()
        assert run["revision"] == 1
    assert scenario.store.agent_run(COMPANY_ID, other)["status"] == "running"
    details = await _kill_audit(scenario)
    assert details["ended_agent_run_ids"] == own
    expected_authority = "founder" if actor == "founder" else "cell_owner_founder_decision_ref"
    assert details["kill_authority"] == expected_authority


@pytest.mark.asyncio
async def test_kill_with_no_runs_records_an_empty_list() -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)

    killed = await _kill(scenario, "founder", revision)

    assert killed["state"]["ended_agent_run_ids"] == []
    assert (await _kill_audit(scenario))["ended_agent_run_ids"] == []


@pytest.mark.asyncio
async def test_refused_kill_leaves_runs_running() -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)
    own, _ = _seed_runs(scenario)

    with pytest.raises(Exception):  # noqa: B017 - owner without a founder decision
        await _kill(scenario, "owner", revision, founder_decision_ref=None)

    assert {scenario.store.agent_run(COMPANY_ID, run_id)["status"] for run_id in own} == {"running"}


async def _killed_cell_with_leftover_runs(scenario: Scenario) -> list[str]:
    """A cell killed before kills ended runs: its runs are still running."""

    revision = await _paused_cell(scenario)
    await _kill(scenario, "founder", revision)
    own, _ = _seed_runs(scenario)
    return own


async def _maintenance(
    scenario: Scenario, actor: str, payload: dict[str, object], key: str
) -> dict[str, object]:
    # maintenance.run records no domain event (only an audit row), so it does not
    # use Scenario.invoke, which asserts an event and a revision bump.
    return dict(
        await scenario.service.invoke(
            "maintenance.run",
            scenario.principals[actor],
            mutation("maintenance.run", payload, expected_revision=0, key=key),
        )
    )


@pytest.mark.asyncio
async def test_maintenance_run_is_report_only_by_default() -> None:
    scenario = Scenario()
    own = await _killed_cell_with_leftover_runs(scenario)

    result = await _maintenance(scenario, "founder", {}, "maint-report")

    state = result["state"]
    assert state["ended_agent_run_ids"] == []
    assert state["report"]["counts"]["agent_run_on_killed_cell"] == len(own)
    assert {scenario.store.agent_run(COMPANY_ID, r)["status"] for r in own} == {"running"}


@pytest.mark.asyncio
async def test_founder_maintenance_run_ends_runs_on_killed_cells_idempotently() -> None:
    scenario = Scenario()
    own, other = await _killed_cell_with_leftover_runs(scenario), stable_uuid("other-cell-run")
    payload = {"end_runs_on_killed_cells": True}

    result = await _maintenance(scenario, "founder", payload, "maint-end")

    assert result["state"]["ended_agent_run_ids"] == own
    assert result["state"]["ended_agent_run_ids_by_cell"] == {CELL_ID: own}
    for run_id in own:
        run = scenario.store.agent_run(COMPANY_ID, run_id)
        assert (run["status"], run["termination_code"]) == ("revoked", "cell_killed")
    assert scenario.store.agent_run(COMPANY_ID, other)["status"] == "running"
    export = await scenario.store.export_company(COMPANY_ID)
    audit = [item for item in export["audit_events"] if item["action"] == "maintenance.run"]
    assert len(audit) == 1
    assert audit[0]["details"]["ended_agent_run_ids"] == own
    assert audit[0]["details"]["end_runs_on_killed_cells"] is True

    replay = await _maintenance(scenario, "founder", payload, "maint-end")
    assert replay["replayed"] is True
    assert replay["state"]["ended_agent_run_ids"] == own
    second = await _maintenance(scenario, "founder", payload, "maint-end-2")
    assert second["state"]["ended_agent_run_ids"] == []


@pytest.mark.asyncio
async def test_cell_owner_maintenance_run_is_limited_to_its_own_cell() -> None:
    scenario = Scenario()
    own = await _killed_cell_with_leftover_runs(scenario)

    with pytest.raises(AuthorizationError):
        await _maintenance(scenario, "owner", {"end_runs_on_killed_cells": True}, "o-1")
    with pytest.raises(AuthorizationError):
        await _maintenance(
            scenario, "evaluator", {"cell_id": CELL_ID, "end_runs_on_killed_cells": True}, "e-1"
        )
    result = await _maintenance(
        scenario, "owner", {"cell_id": CELL_ID, "end_runs_on_killed_cells": True}, "o-2"
    )
    assert result["state"]["ended_agent_run_ids"] == own
    assert result["state"]["report"]["scoped_to_cell_id"] == CELL_ID


@pytest.mark.asyncio
async def test_maintenance_run_rejects_unknown_fields_and_bad_flags() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    with pytest.raises(ValidationError):
        await _maintenance(scenario, "founder", {"mode": "mutate"}, "bad-1")
    with pytest.raises(ValidationError):
        await _maintenance(scenario, "founder", {"end_runs_on_killed_cells": "yes"}, "bad-2")
    # Active cells never have their runs ended.
    result = await _maintenance(scenario, "founder", {"end_runs_on_killed_cells": True}, "ok")
    assert result["state"]["ended_agent_run_ids"] == []


@pytest.mark.asyncio
async def test_protocol_describe_advertises_on_demand_scan_and_kill_contract() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    protocol = await scenario.service.invoke(
        "protocol.describe", scenario.principals["founder"], {}
    )
    tick = protocol["runtime_tick"]
    assert tick["scheduled"] is None
    assert tick["on_demand"]["tool"] == "maintenance.run"
    assert "same transaction" in tick["cell_kill_ends_agent_runs"]
    assert "maintenance.run" in str(protocol.get("tools", ""))


@pytest.mark.asyncio
async def test_kill_of_a_heavily_populated_cell_bounds_the_event_payload() -> None:
    from ai_nature_company.service import KILL_EVENT_RUN_ID_LIMIT

    scenario = Scenario()
    revision = await _paused_cell(scenario)
    now = scenario.clock()
    run_ids = []
    for index in range(1000):
        run_id = stable_uuid(f"bulk-run-{index}")
        scenario.store.seed_agent_run(
            COMPANY_ID,
            agent_run_id=run_id,
            agent_instance_id=stable_uuid(f"bulk-instance-{index}"),
            role="specialist",
            cell_id=CELL_ID,
            started_at=now - timedelta(hours=1),
            expires_at=now + timedelta(days=10),
        )
        run_ids.append(run_id)

    killed = await _kill(scenario, "founder", revision)

    assert killed["state"]["ended_agent_run_ids"] == sorted(run_ids)
    assert (await _kill_audit(scenario))["ended_agent_run_ids"] == sorted(run_ids)
    export = await scenario.store.export_company(COMPANY_ID)
    event = next(
        item for item in reversed(export["domain_events"]) if item["event_type"] == "cell.killed"
    )
    assert event["payload"]["ended_agent_run_count"] == 1000
    assert len(event["payload"]["ended_agent_run_ids"]) == KILL_EVENT_RUN_ID_LIMIT
    assert event["payload"]["ended_agent_run_ids_truncated"] is True

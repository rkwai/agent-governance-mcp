"""Cell-owner observability: work audit history, claim/lease fields, named create errors."""

from __future__ import annotations

import json

import pytest

from ai_nature_company.errors import ValidationError

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, PRINCIPAL_IDS, WORK_ITEM_ID, mutation, stable_uuid

FOREIGN_CELL_ID = stable_uuid("foreign-cell-for-work-observability")
HISTORICAL_WORK_AUDIT_ID = stable_uuid("historical-work-audit-without-cell")


def _claim_fields(record: dict[str, object]) -> dict[str, object]:
    return {
        "claimant_agent_id": record.get("claimant_agent_id"),
        "lease_id": record.get("lease_id"),
        "lease_expires_at": record.get("lease_expires_at"),
        "last_heartbeat_at": record.get("last_heartbeat_at"),
    }


def _never_claimed() -> dict[str, None]:
    return {
        "claimant_agent_id": None,
        "lease_id": None,
        "lease_expires_at": None,
        "last_heartbeat_at": None,
    }


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "work_item_id": stable_uuid("invalid-work-create"),
        "cell_id": CELL_ID,
        "experiment_id": None,
        "kind": "collect_verified_sample",
        "outcome_contract": {
            "outcome": "Record one attributable customer response.",
            "acceptance_criteria": ["Evidence cites the fake CRM receipt."],
            "result_schema_ref": "schemas/evidence-v1.json",
        },
        "eligible_capabilities": ["evidence.append"],
        "budget": {"currency": "USD", "maximum_cost_minor": 1_000},
        "approval_requirements": ["none"],
        "input_references": [],
        "max_attempts": 3,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_cell_scoped_audit_list_includes_work_events_for_own_cell() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    claimed = await scenario.claim_work()
    await scenario.invoke(
        "work.heartbeat",
        "specialist",
        {
            "work_item_id": WORK_ITEM_ID,
            "lease_token": claimed["state"]["lease"]["token"],
            "lease_seconds": 60,
        },
        expected_revision=scenario.work_item_revision(WORK_ITEM_ID),
        key="heartbeat-for-audit",
    )

    owner_audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["owner"],
        {"limit": 1_000},
    )
    actions = {item["action"] for item in owner_audit["audit_events"]}
    assert {"work.create", "work.claim", "work.heartbeat"} <= actions
    work_events = [
        item for item in owner_audit["audit_events"] if str(item["action"]).startswith("work.")
    ]
    assert work_events
    assert all(
        item.get("details", {}).get("cell_id") == CELL_ID or item.get("resource_id") == WORK_ITEM_ID
        for item in work_events
    )

    async with scenario.store.transaction(COMPANY_ID) as tx:
        await tx.append_audit_event(
            audit_id=stable_uuid("foreign-work-audit"),
            action="work.create",
            outcome="committed",
            actor_id=scenario.principals["owner"].principal_id,
            resource_type="work_item",
            resource_id=stable_uuid("foreign-work-item"),
            details={"cell_id": FOREIGN_CELL_ID},
        )
        await tx.append_audit_event(
            audit_id=HISTORICAL_WORK_AUDIT_ID,
            action="work.fail",
            outcome="committed",
            actor_id=scenario.principals["specialist"].principal_id,
            resource_type="work_item",
            resource_id=WORK_ITEM_ID,
            details={"operation_id": "historical-without-cell"},
        )

    scoped = await scenario.service.invoke(
        "audit.list",
        scenario.principals["owner"],
        {"limit": 1_000},
    )
    scoped_actions = [item["action"] for item in scoped["audit_events"]]
    assert "work.fail" in scoped_actions
    assert HISTORICAL_WORK_AUDIT_ID in {item["audit_id"] for item in scoped["audit_events"]}
    assert all(
        item.get("details", {}).get("cell_id") != FOREIGN_CELL_ID for item in scoped["audit_events"]
    )
    assert stable_uuid("foreign-work-item") not in {
        item.get("resource_id") for item in scoped["audit_events"]
    }


@pytest.mark.asyncio
async def test_work_records_expose_claimant_lease_and_last_heartbeat() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    created = await scenario.create_work()
    created_work = created["state"]["work_item"]
    assert _claim_fields(created_work) == _never_claimed()

    page = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "work_item", "limit": 50}},
    )
    listed = next(
        item["state"]
        for item in page["cell_records"]["records"]
        if item["record_id"] == WORK_ITEM_ID
    )
    assert _claim_fields(listed) == _never_claimed()

    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    summary = next(item for item in viewed["work_items"] if item["work_item_id"] == WORK_ITEM_ID)
    assert summary["status"] == "open"
    assert _claim_fields(summary) == _never_claimed()

    claimed = await scenario.claim_work(lease_seconds=120)
    claimed_work = claimed["state"]["work_item"]
    lease = claimed["state"]["lease"]
    assert claimed_work["claimant_agent_id"] == PRINCIPAL_IDS["specialist"]
    assert claimed_work["lease_id"] == lease["lease_id"]
    assert claimed_work["lease_expires_at"] == lease["expires_at"]
    assert claimed_work["last_heartbeat_at"] == lease["updated_at"]
    serialized = json.dumps(claimed_work)
    assert lease["token"] not in serialized
    assert "lease_token" not in serialized

    claimed_at = claimed_work["last_heartbeat_at"]
    scenario.clock.advance(seconds=15)
    heartbeated = await scenario.invoke(
        "work.heartbeat",
        "specialist",
        {
            "work_item_id": WORK_ITEM_ID,
            "lease_token": lease["token"],
            "lease_seconds": 180,
        },
        expected_revision=scenario.work_item_revision(WORK_ITEM_ID),
        key="heartbeat-for-observation",
    )
    renewed = heartbeated["state"]["work_item"]
    assert renewed["claimant_agent_id"] == PRINCIPAL_IDS["specialist"]
    assert renewed["lease_id"] == lease["lease_id"]
    assert renewed["last_heartbeat_at"] != claimed_at
    assert renewed["lease_expires_at"] == heartbeated["state"]["lease"]["expires_at"]

    after = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "work_item", "limit": 50}},
    )
    listed_claimed = next(
        item["state"]
        for item in after["cell_records"]["records"]
        if item["record_id"] == WORK_ITEM_ID
    )
    assert _claim_fields(listed_claimed) == _claim_fields(renewed)
    assert lease["token"] not in json.dumps(after)

    cell = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    claimed_summary = next(
        item for item in cell["work_items"] if item["work_item_id"] == WORK_ITEM_ID
    )
    assert claimed_summary["status"] == "claimed"
    assert _claim_fields(claimed_summary) == _claim_fields(renewed)


@pytest.mark.asyncio
async def test_work_create_validation_names_failing_fields() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()

    missing_kind = _create_payload()
    del missing_kind["kind"]
    with pytest.raises(ValidationError, match="kind is required") as missing:
        await scenario.service.invoke(
            "work.create",
            scenario.principals["owner"],
            mutation(
                "work.create",
                missing_kind,
                expected_revision=0,
                key="create-missing-kind",
            ),
        )
    assert "kind is required" in str(missing.value)
    assert "validation error for WorkItem" not in str(missing.value)

    with pytest.raises(ValidationError, match="kind:") as invalid_kind:
        await scenario.service.invoke(
            "work.create",
            scenario.principals["owner"],
            mutation(
                "work.create",
                _create_payload(kind="Not A Kind"),
                expected_revision=0,
                key="create-invalid-kind",
            ),
        )
    assert "kind:" in str(invalid_kind.value)

    with pytest.raises(ValidationError, match="max_attempts:") as invalid_attempts:
        await scenario.service.invoke(
            "work.create",
            scenario.principals["owner"],
            mutation(
                "work.create",
                _create_payload(max_attempts=0),
                expected_revision=0,
                key="create-invalid-attempts",
            ),
        )
    assert "max_attempts:" in str(invalid_attempts.value)

    with pytest.raises(ValidationError, match="outcome_contract") as missing_contract:
        await scenario.service.invoke(
            "work.create",
            scenario.principals["owner"],
            mutation(
                "work.create",
                _create_payload(outcome_contract="not-an-object"),
                expected_revision=0,
                key="create-invalid-contract",
            ),
        )
    assert "outcome_contract" in str(missing_contract.value)

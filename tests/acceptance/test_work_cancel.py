"""Owner-signed work.cancel: close never-claimed work on the owner's own cell."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ai_nature_company.errors import AuthorizationError, ConflictError, ValidationError

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, WORK_ITEM_ID, stable_uuid

CANCEL_REASON = (
    "Never-claimed pre-Gate-1 scout work is overdue and superseded by the current Sense lanes."
)
SUPERSEDED_BY = stable_uuid("superseding-sense-work")
FOREIGN_CELL_ID = stable_uuid("foreign-cell-for-work-cancel")
FOREIGN_WORK_ID = stable_uuid("foreign-work-for-work-cancel")
OVERDUE_WORK_ID = stable_uuid("owner-cancel-overdue-work")


async def _running_experiment() -> Scenario:
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


async def _create_overdue_open_work(
    scenario: Scenario,
    *,
    work_item_id: str = OVERDUE_WORK_ID,
    key: str = "create-overdue-work",
) -> dict[str, object]:
    deadline = (scenario.clock() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    created = await scenario.create_work(
        work_item_id=work_item_id,
        budget={"unit": "tries", "maximum_cost_minor": 1},
        deadline_at=deadline,
        key=key,
    )
    scenario.clock.advance(hours=2)
    return created


def _payload(
    *,
    work_item_id: str = OVERDUE_WORK_ID,
    cell_id: str = CELL_ID,
    reason: str | None = CANCEL_REASON,
    superseded_by: str | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "work_item_id": work_item_id,
        "cell_id": cell_id,
    }
    if reason is not None:
        payload["reason"] = reason
    if superseded_by is not None:
        payload["superseded_by"] = superseded_by
    if extra:
        payload.update(extra)
    return payload


async def _cancel(
    scenario: Scenario,
    actor: str,
    payload: dict[str, object],
    *,
    expected_revision: int,
    key: str = "cancel-work",
) -> dict[str, object]:
    return await scenario.invoke(
        "work.cancel",
        actor,
        payload,
        expected_revision=expected_revision,
        key=key,
    )


def _cell_stale_count(described: dict) -> int:
    cell = next(item for item in described["portfolio"]["cells"] if item["cell_id"] == CELL_ID)
    return int(cell["stale_open_work_count"])


@pytest.mark.asyncio
async def test_owner_cancels_never_claimed_overdue_work_on_own_cell() -> None:
    scenario = await _running_experiment()
    created = await _create_overdue_open_work(scenario)
    before = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    assert _cell_stale_count(before) == 1

    cancelled = await _cancel(
        scenario,
        "owner",
        _payload(superseded_by=SUPERSEDED_BY),
        expected_revision=1,
        key="cancel-overdue",
    )

    work = cancelled["state"]["work_item"]
    assert cancelled["prior_revision"] == 1
    assert cancelled["new_revision"] == 2
    assert work["work_item_id"] == OVERDUE_WORK_ID
    assert work["status"] == "cancelled"
    assert work["cell_id"] == CELL_ID
    history = work["cancellation_history"]
    assert len(history) == 1
    record = history[0]
    assert record["reason"] == CANCEL_REASON
    assert record["superseded_by"] == SUPERSEDED_BY
    assert record["cancelled_by_principal_id"] == scenario.principals["owner"].principal_id
    assert record["cancelled_at"]

    decision = cancelled["state"]["decision"]
    explained = await scenario.service.invoke(
        "decision.explain",
        scenario.principals["owner"],
        {"decision_id": decision["decision_id"]},
    )
    shown = explained["decision"]
    assert shown["kind"] == "work.cancel"
    assert shown["reason"] == CANCEL_REASON
    assert shown["superseded_by"] == SUPERSEDED_BY
    assert shown["work_item_id"] == OVERDUE_WORK_ID

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["action"] == "work.cancel")
    assert event["details"]["reason"] == CANCEL_REASON
    assert event["details"]["superseded_by"] == SUPERSEDED_BY
    assert event["details"]["work_item_id"] == OVERDUE_WORK_ID
    assert event["details"]["cell_id"] == CELL_ID
    assert event["details"]["decision_id"] == decision["decision_id"]

    page = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "work_item", "limit": 50}},
    )
    stored = next(
        item["state"]
        for item in page["cell_records"]["records"]
        if item["record_id"] == OVERDUE_WORK_ID
    )
    assert stored["status"] == "cancelled"
    assert stored["cancellation_history"][-1]["reason"] == CANCEL_REASON

    after = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    assert _cell_stale_count(after) == 0
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    assert viewed["stale_open_work_count"] == 0
    assert created["state"]["work_item"]["status"] == "open"


@pytest.mark.asyncio
async def test_non_owner_claimed_other_cell_and_missing_reason_are_refused() -> None:
    scenario = await _running_experiment()
    await _create_overdue_open_work(scenario)

    with pytest.raises(AuthorizationError):
        await _cancel(
            scenario,
            "specialist",
            _payload(),
            expected_revision=1,
            key="cancel-as-specialist",
        )
    with pytest.raises(AuthorizationError):
        await _cancel(
            scenario,
            "evaluator",
            _payload(),
            expected_revision=1,
            key="cancel-as-evaluator",
        )

    async with scenario.store.transaction(COMPANY_ID) as tx:
        record = await tx.get_projection("work_item", OVERDUE_WORK_ID)
        assert record is not None
        foreign = dict(record["state"])
        foreign["work_item_id"] = FOREIGN_WORK_ID
        foreign["cell_id"] = FOREIGN_CELL_ID
        await tx.put_projection(
            projection_type="work_item",
            projection_id=FOREIGN_WORK_ID,
            state=foreign,
            expected_revision=0,
            updated_at=scenario.clock(),
        )
        await tx.put_projection(
            projection_type="work_runtime",
            projection_id=FOREIGN_WORK_ID,
            state={"attempt": 0, "lease_id": None, "result_evidence_ids": []},
            expected_revision=0,
            updated_at=scenario.clock(),
        )

    with pytest.raises(AuthorizationError, match="does not cover this cell"):
        await _cancel(
            scenario,
            "owner",
            _payload(work_item_id=FOREIGN_WORK_ID, cell_id=FOREIGN_CELL_ID),
            expected_revision=1,
            key="cancel-foreign-cell",
        )
    with pytest.raises(ValidationError, match="cell_id does not match work item"):
        await _cancel(
            scenario,
            "owner",
            _payload(work_item_id=FOREIGN_WORK_ID, cell_id=CELL_ID),
            expected_revision=1,
            key="cancel-mismatched-cell",
        )

    with pytest.raises(ValidationError, match="reason is required"):
        await scenario.invoke(
            "work.cancel",
            "owner",
            {"work_item_id": OVERDUE_WORK_ID, "cell_id": CELL_ID},
            expected_revision=1,
            key="cancel-missing-reason",
        )
    with pytest.raises(ValidationError, match="reason is required"):
        await _cancel(
            scenario,
            "owner",
            _payload(reason="   "),
            expected_revision=1,
            key="cancel-blank-reason",
        )

    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    assert _cell_stale_count(described) == 1


@pytest.mark.asyncio
async def test_claimed_or_leased_work_is_refused() -> None:
    scenario = await _running_experiment()
    deadline = (scenario.clock() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await scenario.create_work(
        work_item_id=WORK_ITEM_ID,
        budget={"unit": "tries", "maximum_cost_minor": 1},
        deadline_at=deadline,
        key="create-claimed-work",
    )
    await scenario.claim_work(work_item_id=WORK_ITEM_ID, key="claim-to-block-cancel")

    with pytest.raises(ValidationError, match="status is claimed"):
        await _cancel(
            scenario,
            "owner",
            _payload(work_item_id=WORK_ITEM_ID),
            expected_revision=2,
            key="cancel-claimed",
        )


@pytest.mark.asyncio
async def test_cancel_replay_is_idempotent_and_stale_revision_is_refused() -> None:
    scenario = await _running_experiment()
    await _create_overdue_open_work(scenario)
    payload = _payload()

    first = await _cancel(
        scenario,
        "owner",
        payload,
        expected_revision=1,
        key="cancel-once",
    )
    replay = await _cancel(
        scenario,
        "owner",
        payload,
        expected_revision=1,
        key="cancel-once",
    )
    assert replay["replayed"] is True
    assert replay["new_revision"] == first["new_revision"]
    assert replay["state"]["work_item"]["status"] == "cancelled"
    assert (
        replay["state"]["work_item"]["cancellation_history"]
        == first["state"]["work_item"]["cancellation_history"]
    )

    with pytest.raises(ConflictError, match="stale"):
        await _cancel(
            scenario,
            "owner",
            payload,
            expected_revision=1,
            key="cancel-stale",
        )
    with pytest.raises(ValidationError, match="status is cancelled"):
        await _cancel(
            scenario,
            "owner",
            payload,
            expected_revision=2,
            key="cancel-already-terminal",
        )


@pytest.mark.asyncio
async def test_protocol_describe_advertises_owner_signed_work_cancel() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["owner"],
        {},
    )
    assert "work.cancel" in described["tools"]
    workflow = described["bet_workflow"]
    cancel = workflow["work_cancel"]
    assert cancel["tool"] == "work.cancel"
    assert cancel["authority"] == "owner_signed"
    assert cancel["requires"] == "reason"
    assert cancel["valid_state"] == "open"
    assert cancel["own_cell_only"] is True
    assert cancel["never_claimed"] is True
    assert cancel["no_active_lease"] is True
    assert "claimed_or_leased_work" in cancel["must_not"]
    assert any("work.cancel" in step for step in workflow["operate"])
    assert "work.cancel" in described["natural_forces"]["forces"]["maintain"]["tools"]

    owner = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    assert "work.cancel" in owner["principal"]["effective_capabilities"]
    evaluator = await scenario.service.invoke(
        "company.describe",
        scenario.principals["evaluator"],
        {},
    )
    assert "work.cancel" not in evaluator["principal"]["effective_capabilities"]

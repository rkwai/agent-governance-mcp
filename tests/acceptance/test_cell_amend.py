"""Owner-signed cell.amend: revise objective text without touching authority."""

from __future__ import annotations

import pytest

from ai_nature_company.errors import AuthorizationError, ConflictError, ValidationError
from ai_nature_company.models import CellStatus

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, stable_uuid

FOUNDER_DECISION_REF = (
    "Founder decision 2026-09-26 19:50 PT via chief-of-staff agent: "
    "extend sit-the-seat covering window to Wed 2026-10-14 23:59 PT; "
    "all other fences unchanged."
)
FOREIGN_CELL_ID = stable_uuid("foreign-cell-for-cell-amend")

# Live sit-the-seat cell 40bd931e-7f9e-5420-89f7-f94f24f9e070 revision 3
# (read 2026-09-27). Bound replaces only the date token.
LIVE_OBJECTIVE = (
    "Cover a posted FTE seat month-to-month while the customer searches, "
    "using tries and time through 2026-09-30."
)
LIVE_OBJECTIVE_AMENDED = (
    "Cover a posted FTE seat month-to-month while the customer searches, "
    "using tries and time through 2026-10-14."
)


async def _amend(
    scenario: Scenario,
    actor: str,
    payload: dict[str, object],
    *,
    expected_revision: int | None = None,
    key: str = "amend-cell",
) -> dict[str, object]:
    return await scenario.invoke(
        "cell.amend",
        actor,
        payload,
        expected_revision=(
            scenario.cell_revision if expected_revision is None else expected_revision
        ),
        key=key,
    )


def _payload(
    *,
    objective: str | None = LIVE_OBJECTIVE_AMENDED,
    founder_decision_ref: str = FOUNDER_DECISION_REF,
    cell_id: str = CELL_ID,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "cell_id": cell_id,
        "founder_decision_ref": founder_decision_ref,
    }
    if objective is not None:
        payload["objective"] = objective
    if extra:
        payload.update(extra)
    return payload


@pytest.mark.asyncio
async def test_owner_revises_objective_and_retains_amendment_lineage() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    prior = viewed["cell"]
    prior_objective = prior["objective"]
    prior_budget_id = prior["budget_id"]
    prior_owner = prior["owner_principal_id"]
    prior_state = prior["state"]
    prior_name = prior["name"]
    constitution = await scenario.service.invoke(
        "constitution.get",
        scenario.principals["owner"],
        {},
    )

    amended = await _amend(
        scenario,
        "owner",
        _payload(objective=LIVE_OBJECTIVE_AMENDED),
        key="amend-objective",
    )

    cell = amended["state"]["cell"]
    assert amended["prior_revision"] == prior["revision"]
    assert amended["new_revision"] == prior["revision"] + 1
    assert cell["revision"] == prior["revision"] + 1
    assert cell["cell_id"] == CELL_ID
    assert cell["objective"] == LIVE_OBJECTIVE_AMENDED
    assert cell["budget_id"] == prior_budget_id
    assert cell["owner_principal_id"] == prior_owner
    assert cell["state"] == prior_state
    assert cell["status"] == prior_state
    assert cell["name"] == prior_name
    history = cell["amendment_history"]
    assert len(history) == 1
    record = history[0]
    assert record["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert record["prior"]["objective"] == prior_objective
    assert record["new"]["objective"] == LIVE_OBJECTIVE_AMENDED
    assert record["changed_fields"] == ["objective"]

    decision = amended["state"]["decision"]
    explained = await scenario.service.invoke(
        "decision.explain",
        scenario.principals["owner"],
        {"decision_id": decision["decision_id"]},
    )
    shown = explained["decision"]
    assert shown["kind"] == "cell.amend"
    assert shown["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert shown["prior"]["objective"] == prior_objective
    assert shown["new"]["objective"] == LIVE_OBJECTIVE_AMENDED
    assert shown["changed_fields"] == ["objective"]

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["action"] == "cell.amend")
    assert event["details"]["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert event["details"]["prior"]["objective"] == prior_objective
    assert event["details"]["new"]["objective"] == LIVE_OBJECTIVE_AMENDED
    assert event["details"]["decision_id"] == decision["decision_id"]

    reread = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    assert reread["cell"]["objective"] == LIVE_OBJECTIVE_AMENDED
    assert reread["cell"]["amendment_history"][-1]["prior"]["objective"] == prior_objective

    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    portfolio_cell = next(
        item for item in described["portfolio"]["cells"] if item["cell_id"] == CELL_ID
    )
    assert portfolio_cell["objective"] == LIVE_OBJECTIVE_AMENDED
    assert portfolio_cell["amendment_history"][-1]["founder_decision_ref"] == FOUNDER_DECISION_REF

    after = await scenario.service.invoke(
        "constitution.get",
        scenario.principals["owner"],
        {},
    )
    assert after["constitution"] == constitution["constitution"]


@pytest.mark.asyncio
async def test_amend_refuses_other_cells_missing_ref_stale_revision_and_forbidden_fields() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    current = scenario.cell_revision

    async with scenario.store.transaction(COMPANY_ID) as tx:
        record = await tx.get_projection("cell", CELL_ID)
        assert record is not None
        foreign = dict(record["state"])
        foreign["cell_id"] = FOREIGN_CELL_ID
        await tx.put_projection(
            projection_type="cell",
            projection_id=FOREIGN_CELL_ID,
            state=foreign,
            expected_revision=0,
            updated_at=scenario.clock(),
        )
    with pytest.raises(AuthorizationError, match="does not cover this cell"):
        await _amend(
            scenario,
            "owner",
            _payload(cell_id=FOREIGN_CELL_ID),
            expected_revision=current,
            key="amend-wrong-cell",
        )

    with pytest.raises(ValidationError, match="founder_decision_ref is required"):
        await scenario.invoke(
            "cell.amend",
            "owner",
            {"cell_id": CELL_ID, "objective": LIVE_OBJECTIVE_AMENDED},
            expected_revision=current,
            key="amend-missing-ref",
        )

    with pytest.raises(ConflictError, match="stale"):
        await _amend(
            scenario,
            "owner",
            _payload(),
            expected_revision=0,
            key="amend-stale",
        )

    for extra, key in (
        ({"name": "rewritten"}, "amend-name"),
        ({"budget_id": prior_budget(scenario)}, "amend-budget"),
        ({"state": "paused"}, "amend-state"),
        ({"owner_principal_id": scenario.principals["owner"].principal_id}, "amend-owner"),
        ({"authority_policy": {}}, "amend-authority"),
        ({"constitution": {}}, "amend-constitution"),
    ):
        with pytest.raises(
            ValidationError,
            match="cannot change cell budget, stage, owner, authority, or constitution",
        ):
            await _amend(
                scenario,
                "owner",
                _payload(extra=extra),
                expected_revision=current,
                key=key,
            )


def prior_budget(scenario: Scenario) -> str:
    return str(scenario.responses["cell.create"]["state"]["cell"]["budget_id"])


@pytest.mark.asyncio
async def test_amend_refuses_draft_paused_and_terminal_cells() -> None:
    draft = Scenario()
    await draft.bootstrap()
    await draft.create_cell()
    with pytest.raises(ValidationError, match="only an active non-terminal cell"):
        await _amend(
            draft,
            "owner",
            _payload(),
            expected_revision=draft.cell_revision,
            key="amend-draft",
        )

    scenario = Scenario()
    await scenario.build_offer_cell()
    async with scenario.store.transaction(COMPANY_ID) as tx:
        record = await tx.get_projection("cell", CELL_ID)
        assert record is not None
        killed = dict(record["state"])
        killed["state"] = CellStatus.KILLED.value
        killed["status"] = CellStatus.KILLED.value
        killed["closed_at"] = scenario.clock().isoformat().replace("+00:00", "Z")
        await tx.put_projection(
            projection_type="cell",
            projection_id=CELL_ID,
            state=killed,
            expected_revision=record["revision"],
            updated_at=scenario.clock(),
        )
    with pytest.raises(ValidationError, match="only an active non-terminal cell"):
        await _amend(
            scenario,
            "owner",
            _payload(),
            key="amend-killed",
        )


@pytest.mark.asyncio
async def test_amend_refuses_specialist_evaluator_and_founder() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    for actor, key in (
        ("specialist", "amend-as-specialist"),
        ("evaluator", "amend-as-evaluator"),
        ("founder", "amend-as-founder"),
    ):
        with pytest.raises(AuthorizationError):
            await _amend(
                scenario,
                actor,
                _payload(),
                key=key,
            )


@pytest.mark.asyncio
async def test_amend_is_idempotent_and_rejects_noop() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    payload = _payload()
    first = await _amend(scenario, "owner", payload, key="amend-once")
    replay = await _amend(scenario, "owner", payload, key="amend-once")
    assert replay["replayed"] is True
    assert replay["new_revision"] == first["new_revision"]
    assert replay["state"]["cell"]["objective"] == first["state"]["cell"]["objective"]

    with pytest.raises(ValidationError, match="requires a change to objective"):
        await _amend(
            scenario,
            "owner",
            _payload(objective=str(first["state"]["cell"]["objective"])),
            expected_revision=first["new_revision"],
            key="amend-noop",
        )


@pytest.mark.asyncio
async def test_protocol_describe_advertises_owner_signed_cell_amend() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["owner"],
        {},
    )
    assert "cell.amend" in described["tools"]
    workflow = described["bet_workflow"]
    amend = workflow["cell_amend"]
    assert amend["tool"] == "cell.amend"
    assert amend["authority"] == "owner_signed"
    assert amend["requires"] == "founder_decision_ref"
    assert amend["valid_state"] == "active"
    assert amend["own_cell_only"] is True
    assert amend["may"] == ["revise_objective"]
    assert "change_budget" in amend["must_not"]
    assert "change_authority_or_constitution" in amend["must_not"]
    assert any("cell.amend" in step for step in workflow["operate"])
    assert "cell.amend" in described["natural_forces"]["forces"]["bound"]["tools"]

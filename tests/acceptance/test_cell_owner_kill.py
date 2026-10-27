"""Owner-signed cell kill: the cell owner executes a recorded founder decision."""

from __future__ import annotations

import pytest

from ai_nature_company.errors import AuthorizationError, ValidationError

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID

FOUNDER_DECISION_REF = (
    "Founder decision 2026-10-04 18:33 PT via chief-of-staff agent: "
    "formally kill sit-the-seat cell 40bd931e after failed experiment 80b066da "
    "(evaluation a9099483)."
)
REASON = "Experiment failed: 12 sends, 0 paid covering conversations."


async def _paused_cell(scenario: Scenario) -> int:
    await scenario.build_offer_cell()
    paused = await scenario.invoke(
        "cell.transition",
        "owner",
        {"cell_id": CELL_ID, "target_status": "paused"},
        expected_revision=scenario.cell_revision,
        key="cell-paused",
    )
    return int(paused["state"]["cell"]["revision"])


async def _kill(
    scenario: Scenario,
    actor: str,
    revision: int,
    *,
    key: str = "owner-signed-kill",
    founder_decision_ref: str | None = FOUNDER_DECISION_REF,
    reason: str | None = REASON,
) -> dict[str, object]:
    payload: dict[str, object] = {"cell_id": CELL_ID, "target_status": "killed"}
    if founder_decision_ref is not None:
        payload["founder_decision_ref"] = founder_decision_ref
    if reason is not None:
        payload["reason"] = reason
    return await scenario.invoke(
        "cell.transition",
        actor,
        payload,
        expected_revision=revision,
        key=key,
    )


@pytest.mark.asyncio
async def test_owner_kills_paused_cell_with_founder_decision_ref() -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)

    killed = await _kill(scenario, "owner", revision)

    cell = killed["state"]["cell"]
    assert cell["state"] == "killed"
    assert cell["revision"] == revision + 1
    assert cell["closed_at"] is not None
    assert len(killed["event_ids"]) == 2
    decision = killed["state"]["decision"]
    assert decision["kind"] == "cell.kill"
    assert decision["kill_authority"] == "cell_owner_founder_decision_ref"
    assert decision["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert decision["reason"] == REASON
    assert decision["prior_status"] == "paused"
    assert decision["killed_by_principal_id"] == scenario.principals["owner"].principal_id

    explained = await scenario.service.invoke(
        "decision.explain",
        scenario.principals["owner"],
        {"decision_id": decision["decision_id"]},
    )
    assert explained["decision"]["founder_decision_ref"] == FOUNDER_DECISION_REF

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["audit_id"] == killed["audit_id"])
    assert event["action"] == "cell.transition"
    assert event["details"]["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert event["details"]["kill_authority"] == "cell_owner_founder_decision_ref"
    assert event["details"]["reason"] == REASON
    assert event["details"]["decision_id"] == decision["decision_id"]

    reread = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    assert reread["cell"]["state"] == "killed"

    replay = await _kill(scenario, "owner", revision)
    assert replay["replayed"] is True
    assert replay["audit_id"] == killed["audit_id"]


@pytest.mark.asyncio
async def test_owner_kill_without_founder_decision_ref_stays_founder_only() -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)
    before = await scenario.store.export_company(COMPANY_ID)

    with pytest.raises(AuthorizationError):
        await _kill(scenario, "owner", revision, founder_decision_ref=None, key="no-ref")

    after = await scenario.store.export_company(COMPANY_ID)
    assert after["domain_events"] == before["domain_events"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["specialist", "evaluator", "allocator", "immune"])
async def test_non_owner_roles_cannot_use_founder_decision_ref_to_kill(actor: str) -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)

    with pytest.raises(AuthorizationError):
        await _kill(scenario, actor, revision, key=f"kill-as-{actor}")


@pytest.mark.asyncio
async def test_owner_signed_kill_refuses_live_experiment() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()

    with pytest.raises(ValidationError, match="open experiments: 1"):
        await _kill(scenario, "owner", scenario.cell_revision, key="kill-live-experiment")


@pytest.mark.asyncio
async def test_owner_signed_kill_refuses_open_work() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    await scenario.create_work(experiment_id=None)

    with pytest.raises(ValidationError, match="open or claimed work: 1"):
        await _kill(scenario, "owner", scenario.cell_revision, key="kill-open-work")


@pytest.mark.asyncio
async def test_owner_signed_kill_rejects_blank_founder_decision_ref() -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)

    with pytest.raises(ValidationError, match="founder_decision_ref"):
        await _kill(scenario, "owner", revision, founder_decision_ref="  ", key="blank-ref")


@pytest.mark.asyncio
async def test_founder_kill_still_works_and_records_optional_ref() -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)

    killed = await _kill(scenario, "founder", revision, key="founder-kill")

    assert killed["state"]["cell"]["state"] == "killed"
    assert "decision" not in killed["state"]
    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["audit_id"] == killed["audit_id"])
    assert event["details"]["kill_authority"] == "founder"
    assert event["details"]["founder_decision_ref"] == FOUNDER_DECISION_REF


@pytest.mark.asyncio
async def test_founder_kill_without_ref_is_unchanged() -> None:
    scenario = Scenario()
    revision = await _paused_cell(scenario)

    killed = await _kill(
        scenario,
        "founder",
        revision,
        key="founder-kill-bare",
        founder_decision_ref=None,
        reason=None,
    )

    assert killed["state"]["cell"]["state"] == "killed"
    assert len(killed["event_ids"]) == 1

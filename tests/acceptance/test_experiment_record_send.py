"""Owner-signed experiment.record_send: one founder-signed send spends one try."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from ai_nature_company.errors import (
    AuthorizationError,
    BudgetError,
    ConflictError,
    InternalError,
    ValidationError,
)

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, EVIDENCE_ID, EXPERIMENT_ID, stable_uuid
from .test_natural_forces import _cell_ledger, _tries_running_experiment

SEND_ID = stable_uuid("recorded-send-owner-com")
SEND_ID_TWO = stable_uuid("recorded-send-globex")
SEND_EVIDENCE_ID = stable_uuid("recorded-send-evidence")
DRAFT_WORK_ID = stable_uuid("draft-scout-no-send")
SENSE_WORK_ID = stable_uuid("sense-for-send-evidence")
FOUNDER_SIGNOFF = "founder-signoff:founder-signed-acme-first-touch"


async def _ready_to_record(scenario: Scenario | None = None) -> Scenario:
    scenario = scenario or await _tries_running_experiment()
    scenario.clock.advance(days=29)
    await scenario.create_work(
        work_item_id=SENSE_WORK_ID,
        kind="sense_market",
        budget={"unit": "tries", "maximum_cost_minor": 0},
        key="create-sense-for-send",
    )
    await scenario.claim_work(work_item_id=SENSE_WORK_ID, key="claim-sense-for-send")
    await scenario.append_evidence(
        evidence_id=SEND_EVIDENCE_ID,
        evidence_type="other",
        observation={
            "value": 1,
            "unit": "packet_sent",
            "summary": "Founder signed and sent one first-touch.",
        },
        key="append-send-evidence",
    )
    return scenario


def _send_payload(
    *,
    send_id: str = SEND_ID,
    target: str = "Acme",
    channel: str = "email",
    sent_at: str = "2026-09-21T16:00:00Z",
    founder_signoff_ref: str = FOUNDER_SIGNOFF,
    evidence_id: str = SEND_EVIDENCE_ID,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "experiment_id": EXPERIMENT_ID,
        "send_id": send_id,
        "target": target,
        "channel": channel,
        "sent_at": sent_at,
        "founder_signoff_ref": founder_signoff_ref,
        "evidence_id": evidence_id,
    }
    if extra:
        payload.update(extra)
    return payload


async def _record(
    scenario: Scenario,
    actor: str,
    payload: dict[str, object],
    *,
    expected_revision: int = 0,
    key: str = "record-send",
) -> dict[str, object]:
    return await scenario.invoke(
        "experiment.record_send",
        actor,
        payload,
        expected_revision=expected_revision,
        key=key,
    )


@pytest.mark.asyncio
async def test_owner_records_one_send_and_spends_exactly_one_try() -> None:
    scenario = await _ready_to_record()
    before = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert before["ledger"]["spent_minor"] == 0
    assert before["ledger"]["reserved_minor"] == 4
    assert before["cell"]["experiments"][0]["recorded_send_count"] == 0
    assert before["cell"]["experiments"][0]["spent_minor"] == 0
    assert before["cell"]["experiments"][0]["max_external_messages_per_experiment"] == 10
    assert before["cell"]["experiments"][0]["external_messages_remaining"] == 10

    recorded = await _record(scenario, "owner", _send_payload(), key="record-owner-com")
    send = recorded["state"]["send"]
    assert recorded["prior_revision"] == 0
    assert recorded["new_revision"] == 1
    assert recorded["state"]["already_recorded"] is False
    assert recorded["state"]["ledger_spend_minor"] == 1
    assert recorded["state"]["max_external_messages_per_experiment"] == 10
    assert recorded["state"]["external_messages_remaining"] == 9
    assert send["send_id"] == SEND_ID
    assert send["target"] == "Acme"
    assert send["channel"] == "email"
    assert send["sent_at"] == "2026-09-21T16:00:00Z"
    assert send["founder_signoff_ref"] == FOUNDER_SIGNOFF
    assert send["evidence_id"] == SEND_EVIDENCE_ID
    assert send["spend_minor"] == 1
    assert send["revision"] == 1

    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["ledger"]["spent_minor"] == 1
    assert after["ledger"]["reserved_minor"] == 3
    assert after["cell"]["spent_minor"] == 1
    bet = after["cell"]["experiments"][0]
    assert bet["recorded_send_count"] == 1
    assert bet["spent_minor"] == 1
    assert bet["remaining_budget_minor"] == 3
    assert bet["max_external_messages_per_experiment"] == 10
    assert bet["external_messages_remaining"] == 9

    decision = recorded["state"]["decision"]
    explained = await scenario.service.invoke(
        "decision.explain",
        scenario.principals["owner"],
        {"decision_id": decision["decision_id"]},
    )
    shown = explained["decision"]
    assert shown["kind"] == "experiment.record_send"
    assert shown["send_id"] == SEND_ID
    assert shown["founder_signoff_ref"] == FOUNDER_SIGNOFF
    assert shown["spend_minor"] == 1
    assert shown["max_external_messages_per_experiment"] == 10
    assert shown["external_messages_remaining"] == 9

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(
        item for item in audit["audit_events"] if item["action"] == "experiment.record_send"
    )
    assert event["details"]["send_id"] == SEND_ID
    assert event["details"]["spend_minor"] == 1
    assert event["details"]["decision_id"] == decision["decision_id"]
    assert event["details"]["founder_signoff_ref"] == FOUNDER_SIGNOFF
    assert event["details"]["max_external_messages_per_experiment"] == 10
    assert event["details"]["external_messages_remaining"] == 9

    page = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "experiment_send"}},
    )
    records = page["cell_records"]["records"]
    assert len(records) == 1
    assert records[0]["record_id"] == SEND_ID
    assert records[0]["state"]["target"] == "Acme"


@pytest.mark.asyncio
async def test_draft_scout_complete_does_not_spend_and_record_send_does() -> None:
    scenario = await _ready_to_record()
    await scenario.create_work(
        work_item_id=DRAFT_WORK_ID,
        kind="scout_market",
        budget={"unit": "tries", "maximum_cost_minor": 3},
        key="create-draft-scout",
    )
    await scenario.claim_work(work_item_id=DRAFT_WORK_ID, key="claim-draft-scout")
    await scenario.complete_work(
        work_item_id=DRAFT_WORK_ID,
        evidence_id=SEND_EVIDENCE_ID,
        actual_cost={"unit": "tries", "amount_minor": 0},
        key="complete-draft-scout",
    )
    after_draft = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_draft["ledger"]["spent_minor"] == 0

    await _record(scenario, "owner", _send_payload(), key="record-after-draft")
    after_send = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_send["ledger"]["spent_minor"] == 1
    assert after_send["cell"]["experiments"][0]["recorded_send_count"] == 1


@pytest.mark.asyncio
async def test_record_send_is_idempotent_per_send_and_refuses_double_count() -> None:
    scenario = await _ready_to_record()
    payload = _send_payload()
    first = await _record(scenario, "owner", payload, key="record-once")
    replay = await _record(scenario, "owner", payload, key="record-once")
    assert replay["replayed"] is True
    assert replay["state"]["send"]["send_id"] == first["state"]["send"]["send_id"]

    same_send = await _record(scenario, "owner", payload, key="record-same-send-new-key")
    assert same_send["state"]["already_recorded"] is True
    assert same_send["state"]["ledger_spend_minor"] == 0

    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["ledger"]["spent_minor"] == 1
    assert after["cell"]["experiments"][0]["recorded_send_count"] == 1

    with pytest.raises(ConflictError, match="different external send"):
        await _record(
            scenario,
            "owner",
            _send_payload(target="Globex"),
            key="record-same-id-different-target",
        )

    with pytest.raises(ValidationError, match="already recorded"):
        await _record(
            scenario,
            "owner",
            _send_payload(send_id=SEND_ID_TWO),
            key="record-same-fingerprint",
        )
    still = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert still["ledger"]["spent_minor"] == 1


@pytest.mark.asyncio
async def test_record_send_refuses_specialist_evaluator_and_founder() -> None:
    scenario = await _ready_to_record()
    for actor, key in (
        ("specialist", "record-as-specialist"),
        ("evaluator", "record-as-evaluator"),
        ("founder", "record-as-founder"),
    ):
        with pytest.raises(AuthorizationError):
            await _record(scenario, actor, _send_payload(), key=key)


@pytest.mark.asyncio
async def test_record_send_rejects_non_running_exhausted_and_future_sent_at() -> None:
    scenario = await _ready_to_record()
    with pytest.raises(ValidationError, match="must not be in the future"):
        await _record(
            scenario,
            "owner",
            _send_payload(sent_at=(scenario.clock() + timedelta(days=1)).isoformat()),
            key="record-future",
        )

    for _index in range(4):
        await _record(
            scenario,
            "owner",
            _send_payload(
                send_id=stable_uuid(f"budget-send-{_index}"),
                target=f"Target {_index}",
                sent_at=f"2026-09-2{_index}T16:00:00Z",
            ),
            key=f"record-budget-{_index}",
        )
    exhausted = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert exhausted["ledger"]["spent_minor"] == 4
    with pytest.raises(BudgetError, match="remaining experiment reservation"):
        await _record(
            scenario,
            "owner",
            _send_payload(
                send_id=stable_uuid("over-budget-send"),
                target="Over Budget Co",
                sent_at="2026-09-25T16:00:00Z",
            ),
            key="record-over-budget",
        )

    closed = Scenario()
    await closed.build_evaluating_experiment()
    with pytest.raises(ValidationError, match="only a running experiment"):
        await _record(
            closed,
            "owner",
            _send_payload(evidence_id=EVIDENCE_ID),
            key="record-closed",
        )


@pytest.mark.asyncio
async def test_record_send_rejects_missing_ref_and_foreign_evidence() -> None:
    scenario = await _ready_to_record()
    with pytest.raises(ValidationError, match="founder_signoff_ref is required"):
        await scenario.invoke(
            "experiment.record_send",
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "send_id": SEND_ID,
                "target": "Acme",
                "channel": "email",
                "sent_at": "2026-09-21T16:00:00Z",
                "evidence_id": SEND_EVIDENCE_ID,
            },
            expected_revision=0,
            key="record-missing-ref",
        )
    with pytest.raises(ValidationError, match="unsupported fields"):
        await _record(
            scenario,
            "owner",
            _send_payload(extra={"hypothesis": "rewrite"}),
            key="record-frozen-field",
        )


@pytest.mark.asyncio
async def test_protocol_describe_advertises_owner_signed_record_send() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["owner"],
        {},
    )
    assert "experiment.record_send" in described["tools"]
    assert "experiment_send" in described["bet_workflow"]["context_read"]["kinds"]
    record = described["bet_workflow"]["experiment_record_send"]
    assert record["tool"] == "experiment.record_send"
    assert record["authority"] == "owner_signed"
    assert record["spend"] == "exactly_one_try"
    assert record["idempotent_per"] == "send_id"
    assert record["force"] == "act"
    assert record["enforces"] == "experiment_or_constitution_message_cap"
    assert record["remaining_field"] == "external_messages_remaining"
    assert record["refuses"] == ["double_count", "external_message_cap"]
    assert "cross_validate_sent_at_against_evidence_timestamps" in record["does_not"]
    assert "company.describe cell_records kind experiment_send" in record["read_back"]
    assert "cell-scoped audit.list" in record["read_back"]
    assert described["natural_forces"]["forces"]["act"]["recorded_external_send"] == (
        "exactly_one_try"
    )
    assert described["natural_forces"]["forces"]["act"]["external_message_cap"] == (
        "max_external_messages_per_experiment"
    )
    assert "experiment.record_send" in described["natural_forces"]["forces"]["act"]["tools"]
    assert any("experiment.record_send" in step for step in described["bet_workflow"]["operate"])


async def _amend_message_cap(scenario: Scenario, cap: int) -> None:
    constitution = (
        await scenario.service.invoke("constitution.get", scenario.principals["founder"], {})
    )["constitution"]
    await scenario.invoke(
        "constitution.amend",
        "founder",
        {
            "budget_policy": constitution["budget_policy"],
            "risk_policy": {
                **constitution["risk_policy"],
                "max_external_messages_per_experiment": cap,
            },
        },
        expected_revision=int(constitution["version"]),
        key=f"amend-message-cap-{cap}",
    )


@pytest.mark.asyncio
async def test_record_send_refuses_at_cap_and_names_remaining() -> None:
    scenario = await _ready_to_record()
    await _amend_message_cap(scenario, 1)
    before = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    bet = before["cell"]["experiments"][0]
    assert bet["max_external_messages_per_experiment"] == 1
    assert bet["external_messages_remaining"] == 1
    assert bet["recorded_send_count"] == 0

    first = await _record(scenario, "owner", _send_payload(), key="record-under-cap")
    assert first["state"]["ledger_spend_minor"] == 1
    assert first["state"]["external_messages_remaining"] == 0
    assert first["state"]["max_external_messages_per_experiment"] == 1

    after_first = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after_first["ledger"]["spent_minor"] == 1
    assert after_first["cell"]["experiments"][0]["external_messages_remaining"] == 0
    assert after_first["cell"]["experiments"][0]["recorded_send_count"] == 1

    with pytest.raises(ValidationError, match=r"external message cap reached \(0 of 1 remaining\)"):
        await _record(
            scenario,
            "owner",
            _send_payload(
                send_id=SEND_ID_TWO,
                target="Globex",
                sent_at="2026-09-22T16:00:00Z",
            ),
            key="record-over-cap",
        )
    still = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert still["ledger"]["spent_minor"] == 1
    assert still["cell"]["experiments"][0]["recorded_send_count"] == 1
    assert still["cell"]["experiments"][0]["external_messages_remaining"] == 0

    replay = await _record(scenario, "owner", _send_payload(), key="replay-at-cap")
    assert replay["state"]["already_recorded"] is True
    assert replay["state"]["ledger_spend_minor"] == 0
    assert replay["state"]["external_messages_remaining"] == 0
    replayed = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert replayed["ledger"]["spent_minor"] == 1


@pytest.mark.asyncio
async def test_record_send_fails_closed_when_cap_is_unreadable() -> None:
    scenario = await _ready_to_record()
    async with scenario.store.transaction(COMPANY_ID) as tx:
        company = await tx.get_projection("company", COMPANY_ID)
        assert company is not None
        constitution_id = str(company["state"]["active_constitution_id"])
        constitution = await tx.get_projection("constitution", constitution_id)
        assert constitution is not None
        state = dict(constitution["state"])
        risk = dict(state["risk_policy"])
        del risk["max_external_messages_per_experiment"]
        state["risk_policy"] = risk
        await tx.put_projection(
            projection_type="constitution",
            projection_id=constitution_id,
            state=state,
            expected_revision=constitution["revision"],
        )
    with pytest.raises(ValidationError, match="non-negative integer"):
        await _record(scenario, "owner", _send_payload(), key="record-unreadable-cap")
    still = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert still["ledger"]["spent_minor"] == 0
    assert still["cell"]["experiments"][0]["recorded_send_count"] == 0
    assert "external_messages_remaining" not in still["cell"]["experiments"][0]


# Exact 2026-09-26 owner backfill payloads that crashed in production.
LIVE_EXPERIMENT_ID = "80b066da-b102-5017-9375-797688db6f3f"
LIVE_SHARED_EVIDENCE_ID = "73dadf3e-d7cf-4000-8000-80b066daa002"
LIVE_BACKFILL = (
    {
        "target": "buyer1@acme.example.test",
        "send_id": "80b066da-0001-4000-8000-73dadf3ea002",
        "sent_at": "2026-09-21T03:22:29Z",
        "evidence_id": LIVE_SHARED_EVIDENCE_ID,
        "founder_signoff_ref": ("gmail:<thread-id> founder@example.test -> buyer1@acme.example.test"),
        "idempotency_key": "record-send:owner-com:1a0c1fc8e6bdf0a1",
    },
    {
        "target": "buyer2@globex.example.test",
        "send_id": "80b066da-0002-4000-8000-73dadf3ea002",
        "sent_at": "2026-09-21T03:22:29Z",
        "evidence_id": LIVE_SHARED_EVIDENCE_ID,
        "founder_signoff_ref": ("gmail:<thread-id> founder@example.test -> buyer2@globex.example.test"),
        "idempotency_key": "record-send:globex:1a0c1fc8c8f0b9aa",
    },
    {
        "target": "buyer3@initech.example.test",
        "send_id": "80b066da-0003-4000-8000-f5c0a7b9c002",
        "sent_at": "2026-09-23T02:13:56Z",
        "evidence_id": "f5c0a7b9-2d96-4000-8000-80b066dac002",
        "founder_signoff_ref": "gmail:<thread-id> founder@example.test -> buyer3@initech.example.test",
        "idempotency_key": "record-send:initech:1a0cc0a82ccf01e8",
    },
    {
        "target": "buyer4@hooli.example.test",
        "send_id": "80b066da-0004-4000-8000-b7d3f9a1e002",
        "sent_at": "2026-09-24T03:05:27Z",
        "evidence_id": "b7d3f9a1-4e07-4000-8000-80b066dae002",
        "founder_signoff_ref": ("gmail:<thread-id> founder@example.test -> buyer4@hooli.example.test"),
        "idempotency_key": "record-send:hooli:1a0d1600ace542ac",
    },
    {
        "target": "buyer5@stark.example.test",
        "send_id": "80b066da-0005-4000-8000-d8b3e0f25001",
        "sent_at": "2026-09-12T14:13:43Z",
        "evidence_id": "d8b3e0f2-5c29-4000-8000-80b066da5001",
        "founder_signoff_ref": ("gmail:<thread-id> founder@example.test -> buyer5@stark.example.test"),
        "idempotency_key": "record-send:plug:1a095f788fa297ca",
    },
    {
        "target": "buyer6@umbrella.example.test",
        "send_id": "80b066da-0006-4000-8000-d9f5a1c3f002",
        "sent_at": "2026-09-26T01:52:56Z",
        "evidence_id": "d9f5a1c3-4e07-4000-8000-80b066daf002",
        "founder_signoff_ref": ("gmail:<thread-id> founder@example.test -> buyer6@umbrella.example.test"),
        "idempotency_key": "record-send:umbrella:1a0db6a5d03728e6",
    },
)


def _live_send_payload(row: dict[str, str]) -> dict[str, object]:
    return {
        "experiment_id": LIVE_EXPERIMENT_ID,
        "send_id": row["send_id"],
        "target": row["target"],
        "channel": "email",
        "sent_at": row["sent_at"],
        "founder_signoff_ref": row["founder_signoff_ref"],
        "evidence_id": row["evidence_id"],
    }


async def _production_shaped_amended_experiment() -> Scenario:
    """Amended running experiment, live-like constitution, real-shaped evidence."""

    scenario = Scenario()
    await scenario.bootstrap(
        constitution_unit="tries",
        constitution_currency=None,
        company_limit_minor=400,
        cell_limit_minor=66,
        experiment_limit_minor=66,
        single_action_limit_minor=3,
        budget_unit="tries",
        budget_currency=None,
        budget_limit=400,
    )
    async with scenario.store.transaction(COMPANY_ID) as tx:
        company = await tx.get_projection("company", COMPANY_ID)
        assert company is not None
        constitution_id = str(company["state"]["active_constitution_id"])
        constitution = await tx.get_projection("constitution", constitution_id)
        assert constitution is not None
        state = dict(constitution["state"])
        risk = dict(state["risk_policy"])
        risk["pricing_floor_minor"] = 0
        risk["prohibited_actions"] = ["claim.fabricate"]
        risk["max_external_messages_per_experiment"] = 10
        risk["regulated_market_entry_requires_founder"] = True
        state["risk_policy"] = risk
        await tx.put_projection(
            projection_type="constitution",
            projection_id=constitution_id,
            state=state,
            expected_revision=constitution["revision"],
        )
    await scenario.create_cell(budget_limit=66, unit="tries", currency=None)
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()
    await scenario.register_experiment(
        experiment_id=LIVE_EXPERIMENT_ID,
        budget={"unit": "tries", "amount_minor": 66},
    )
    await scenario.start_experiment(experiment_id=LIVE_EXPERIMENT_ID)
    await scenario.invoke(
        "experiment.amend",
        "owner",
        {
            "experiment_id": LIVE_EXPERIMENT_ID,
            "window_ends_at": "2026-10-15T06:59:59Z",
            "treatment": (
                "Hunter spends tries (max 3 first-touches per weekday) and time "
                "through 2026-10-14; unit of account is tries or time, not packets."
            ),
            "founder_decision_ref": (
                "Founder decision 2026-09-26 19:50 PT via chief-of-staff agent: "
                "extend sit-the-seat covering window to Wed 2026-10-14 23:59 PT; "
                "all other fences unchanged."
            ),
        },
        expected_revision=2,
        key="amend-live-window",
    )
    scenario.clock.current = datetime(2026, 9, 27, 3, 36, tzinfo=UTC)
    await scenario.create_work(
        experiment_id=LIVE_EXPERIMENT_ID,
        work_item_id=SENSE_WORK_ID,
        kind="sense_market",
        budget={"unit": "tries", "maximum_cost_minor": 0},
        key="create-sense-for-live-sends",
    )
    await scenario.claim_work(work_item_id=SENSE_WORK_ID, key="claim-sense-for-live-sends")
    evidence_ids = {row["evidence_id"] for row in LIVE_BACKFILL}
    for evidence_id in sorted(evidence_ids):
        await scenario.append_evidence(
            evidence_id=evidence_id,
            experiment_id=LIVE_EXPERIMENT_ID,
            evidence_type="other",
            observation={
                "value": 1,
                "unit": "packet_sent",
                "summary": "Founder signed first-touch; Gmail sent_at is authoritative.",
            },
            key=f"append-live-evidence:{evidence_id}",
        )
    return scenario


@pytest.mark.asyncio
async def test_live_backfill_payloads_on_amended_experiment_spend_once() -> None:
    scenario = await _production_shaped_amended_experiment()
    page = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "experiment"}},
    )
    experiment = page["cell_records"]["records"][0]["state"]
    assert experiment["experiment_id"] == LIVE_EXPERIMENT_ID
    assert experiment["revision"] == 3
    assert experiment["amendment_history"]
    assert experiment["state"] == "running"

    evidence_page = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "evidence", "limit": 50}},
    )
    evidence_by_id = {
        item["record_id"]: item["state"] for item in evidence_page["cell_records"]["records"]
    }
    shared = evidence_by_id[LIVE_SHARED_EVIDENCE_ID]
    assert shared["observed_at"] == "2026-09-27T03:36:00Z"
    assert shared["observed_at"] != LIVE_BACKFILL[0]["sent_at"]

    for row in LIVE_BACKFILL:
        recorded = await _record(
            scenario,
            "owner",
            _live_send_payload(row),
            key=row["idempotency_key"],
        )
        assert recorded["replayed"] is False
        assert recorded["state"]["already_recorded"] is False
        assert recorded["state"]["ledger_spend_minor"] == 1
        assert recorded["state"]["send"]["sent_at"] == row["sent_at"]
        assert recorded["state"]["send"]["evidence_id"] == row["evidence_id"]
        assert recorded["state"]["send"]["target"] == row["target"]

    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["ledger"]["spent_minor"] == 6
    assert after["cell"]["experiments"][0]["recorded_send_count"] == 6
    assert after["cell"]["experiments"][0]["spent_minor"] == 6
    assert after["cell"]["experiments"][0]["external_messages_remaining"] == 4

    sends = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "experiment_send", "limit": 20}},
    )
    records = sends["cell_records"]["records"]
    assert {item["record_id"] for item in records} == {row["send_id"] for row in LIVE_BACKFILL}
    assert all(item["state"]["decision_id"] for item in records)

    owner_audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["owner"],
        {"limit": 1_000},
    )
    send_events = [
        item for item in owner_audit["audit_events"] if item["action"] == "experiment.record_send"
    ]
    assert len(send_events) == 6
    assert {item["details"]["send_id"] for item in send_events} == {
        row["send_id"] for row in LIVE_BACKFILL
    }
    assert all(item["details"]["cell_id"] == CELL_ID for item in send_events)

    async with scenario.store.transaction(COMPANY_ID) as tx:
        await tx.append_audit_event(
            audit_id=stable_uuid("foreign-cell-audit"),
            action="experiment.record_send",
            outcome="committed",
            actor_id=scenario.principals["owner"].principal_id,
            resource_type="experiment_send",
            resource_id=stable_uuid("foreign-send"),
            details={"cell_id": stable_uuid("other-cell"), "send_id": "foreign"},
        )
    scoped = await scenario.service.invoke(
        "audit.list",
        scenario.principals["owner"],
        {"limit": 1_000},
    )
    assert all(
        item.get("details", {}).get("cell_id") == CELL_ID
        for item in scoped["audit_events"]
        if item["action"] == "experiment.record_send"
    )
    assert "foreign" not in {
        item.get("details", {}).get("send_id") for item in scoped["audit_events"]
    }

    for row in LIVE_BACKFILL:
        replay = await _record(
            scenario,
            "owner",
            _live_send_payload(row),
            key=row["idempotency_key"],
        )
        assert replay["replayed"] is True
        assert replay["state"]["ledger_spend_minor"] == 1
    replayed = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert replayed["ledger"]["spent_minor"] == 6
    assert replayed["cell"]["experiments"][0]["recorded_send_count"] == 6


@pytest.mark.asyncio
async def test_record_send_unexpected_fault_is_structured_and_retry_safe() -> None:
    scenario = await _production_shaped_amended_experiment()
    row = LIVE_BACKFILL[0]
    original = scenario.service._apply_experiment_try_spend

    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated reservation check violation")

    with (
        patch.object(scenario.service, "_apply_experiment_try_spend", new=boom),
        pytest.raises(InternalError, match="internal substrate error") as raised,
    ):
        await _record(
            scenario,
            "owner",
            _live_send_payload(row),
            key=row["idempotency_key"],
        )
    assert raised.value.code == "internal_error"
    failed = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert failed["ledger"]["spent_minor"] == 0
    assert failed["cell"]["experiments"][0]["recorded_send_count"] == 0
    async with scenario.store.transaction(COMPANY_ID) as tx:
        assert await tx.get_projection("experiment_send", row["send_id"]) is None
        claimed = await tx.get_idempotency(
            f"experiment.record_send:{scenario.principals['owner'].principal_id}",
            row["idempotency_key"],
        )
        assert claimed is None

    retry = await _record(
        scenario,
        "owner",
        _live_send_payload(row),
        key=row["idempotency_key"],
    )
    assert retry["replayed"] is False
    assert retry["state"]["ledger_spend_minor"] == 1
    replay = await _record(
        scenario,
        "owner",
        _live_send_payload(row),
        key=row["idempotency_key"],
    )
    assert replay["replayed"] is True
    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["ledger"]["spent_minor"] == 1
    assert after["cell"]["experiments"][0]["recorded_send_count"] == 1
    assert original is not None


@pytest.mark.asyncio
async def test_record_send_honors_experiment_message_cap_override() -> None:
    scenario = await _ready_to_record()
    constitution = await scenario.service.invoke(
        "constitution.get",
        scenario.principals["owner"],
        {},
    )
    assert constitution["constitution"]["risk_policy"]["max_external_messages_per_experiment"] == 10
    await scenario.invoke(
        "experiment.amend",
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "max_external_messages": 1,
            "founder_decision_ref": "founder-decision:cap-override-one",
        },
        expected_revision=2,
        key="amend-override-one",
    )
    first = await _record(scenario, "owner", _send_payload(), key="record-under-override")
    assert first["state"]["max_external_messages_per_experiment"] == 1
    assert first["state"]["external_messages_remaining"] == 0
    with pytest.raises(ValidationError, match=r"external message cap reached \(0 of 1 remaining\)"):
        await _record(
            scenario,
            "owner",
            _send_payload(
                send_id=SEND_ID_TWO,
                target="Globex",
                sent_at="2026-09-22T16:00:00Z",
            ),
            key="record-blocked-by-override",
        )
    still = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert still["cell"]["experiments"][0]["max_external_messages"] == 1
    assert still["cell"]["experiments"][0]["recorded_send_count"] == 1
    assert still["ledger"]["spent_minor"] == 1


@pytest.mark.asyncio
async def test_record_send_honors_amended_budget_and_override() -> None:
    scenario = Scenario()
    await scenario.bootstrap(
        constitution_unit="tries",
        constitution_currency=None,
        company_limit_minor=80,
        cell_limit_minor=40,
        experiment_limit_minor=20,
        single_action_limit_minor=20,
        budget_unit="tries",
        budget_currency=None,
        budget_limit=80,
    )
    await scenario.create_cell(budget_limit=40, unit="tries", currency=None)
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()
    await scenario.register_experiment(budget={"unit": "tries", "amount_minor": 4})
    await scenario.start_experiment()
    scenario.clock.advance(days=29)
    await scenario.create_work(
        work_item_id=SENSE_WORK_ID,
        kind="sense_market",
        budget={"unit": "tries", "maximum_cost_minor": 0},
        key="create-sense-for-both",
    )
    await scenario.claim_work(work_item_id=SENSE_WORK_ID, key="claim-sense-for-both")
    await scenario.append_evidence(
        evidence_id=SEND_EVIDENCE_ID,
        evidence_type="other",
        observation={
            "value": 1,
            "unit": "packet_sent",
            "summary": "Founder signed and sent one first-touch.",
        },
        key="append-send-evidence-both",
    )
    await scenario.invoke(
        "experiment.amend",
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "budget_amount_minor": 6,
            "max_external_messages": 2,
            "founder_decision_ref": "founder-decision:budget-and-cap",
        },
        expected_revision=2,
        key="amend-both-then-send",
    )
    first = await _record(scenario, "owner", _send_payload(), key="record-first-after-both")
    assert first["state"]["max_external_messages_per_experiment"] == 2
    second = await _record(
        scenario,
        "owner",
        _send_payload(send_id=SEND_ID_TWO, target="Globex", sent_at="2026-09-22T16:00:00Z"),
        key="record-second-after-both",
    )
    assert second["state"]["external_messages_remaining"] == 0
    with pytest.raises(ValidationError, match=r"external message cap reached \(0 of 2 remaining\)"):
        await _record(
            scenario,
            "owner",
            _send_payload(
                send_id=stable_uuid("recorded-send-third"),
                target="initech",
                sent_at="2026-09-23T16:00:00Z",
            ),
            key="record-third-blocked",
        )
    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["cell"]["experiments"][0]["remaining_budget_minor"] == 4
    assert after["cell"]["experiments"][0]["max_external_messages"] == 2
    assert after["ledger"]["spent_minor"] == 2
    assert after["ledger"]["reserved_minor"] == 4

"""experiment.close may record optional reason and founder_decision_ref."""

from __future__ import annotations

import pytest

from ai_nature_company.errors import ValidationError

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, EXPERIMENT_ID

CLOSE_REASON = "Founder sunset the outbound window early."
FOUNDER_DECISION_REF = "founder-decision:sunset-sit-the-seat-2026-10-04"


@pytest.mark.asyncio
async def test_experiment_close_persists_optional_reason_and_founder_ref() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    closed = await scenario.close_named_experiment(
        EXPERIMENT_ID,
        reason=CLOSE_REASON,
        founder_decision_ref=FOUNDER_DECISION_REF,
    )
    experiment = closed["state"]["experiment"]
    assert experiment["state"] == "evaluating"
    assert experiment["reason"] == CLOSE_REASON
    assert experiment["founder_decision_ref"] == FOUNDER_DECISION_REF

    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    viewed = described["portfolio"]["cells"][0]["experiments"][0]
    assert viewed["experiment_id"] == EXPERIMENT_ID
    assert viewed["state"] == "evaluating"
    assert viewed["reason"] == CLOSE_REASON
    assert viewed["founder_decision_ref"] == FOUNDER_DECISION_REF

    records = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "experiment"}},
    )
    shown = records["cell_records"]["records"][0]["state"]
    assert shown["reason"] == CLOSE_REASON
    assert shown["founder_decision_ref"] == FOUNDER_DECISION_REF

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["action"] == "experiment.close")
    assert event["details"]["reason"] == CLOSE_REASON
    assert event["details"]["founder_decision_ref"] == FOUNDER_DECISION_REF


@pytest.mark.asyncio
async def test_experiment_close_without_optional_fields_stays_compatible() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    closed = await scenario.close_experiment()
    experiment = closed["state"]["experiment"]
    assert experiment["state"] == "evaluating"
    assert "reason" not in experiment
    assert "founder_decision_ref" not in experiment

    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    viewed = described["portfolio"]["cells"][0]["experiments"][0]
    assert "reason" not in viewed
    assert "founder_decision_ref" not in viewed

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["action"] == "experiment.close")
    assert "reason" not in event["details"]
    assert "founder_decision_ref" not in event["details"]
    export = await scenario.store.export_company(COMPANY_ID)
    assert export["domain_events"]


@pytest.mark.asyncio
async def test_experiment_close_rejects_blank_optional_fields() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    with pytest.raises(ValidationError, match="reason"):
        await scenario.close_named_experiment(
            EXPERIMENT_ID,
            reason="   ",
            key="close-blank-reason",
        )
    with pytest.raises(ValidationError, match="founder_decision_ref"):
        await scenario.close_named_experiment(
            EXPERIMENT_ID,
            founder_decision_ref="",
            key="close-blank-ref",
        )
    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    assert described["portfolio"]["cells"][0]["experiments"][0]["state"] == "running"


@pytest.mark.asyncio
async def test_protocol_describe_advertises_optional_close_fields() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["owner"],
        {},
    )
    close = described["bet_workflow"]["experiment_close"]
    assert close["tool"] == "experiment.close"
    assert close["optional"] == ["reason", "founder_decision_ref"]
    assert close["valid_state"] == "running"
    assert close["backward_compatible_without_optional_fields"] is True
    assert "audit.list" in close["persists_on"]
    assert "company.describe" in close["persists_on"]
    assert any(
        "experiment.close may record optional reason" in step
        for step in described["bet_workflow"]["operate"]
    )

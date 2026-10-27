"""Count criteria may fail with empty evidence_ids when the ledger is honestly empty."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from ai_nature_company.errors import ValidationError

from .scenario import Scenario
from .support import (
    CELL_ID,
    COMPANY_ID,
    EVALUATION_ID,
    EXPERIMENT_ID,
    MUTATED_OFFER_VERSION_ID,
    PRINCIPAL_IDS,
    mutation,
    stable_uuid,
)


def _zero_fail_payload(
    scenario: Scenario,
    *,
    evaluation_id: str = EVALUATION_ID,
    evidence_ids: list[str] | None = None,
    observed_value: int = 0,
    outcome: str = "failed",
    result: str = "failed",
    criterion_id: str = "paid_pilot_requests",
    summary: str = "Zero conversations arrived; the count criterion failed.",
) -> dict[str, Any]:
    experiment_id = EXPERIMENT_ID
    window_ends_at = scenario.responses[f"experiment.close:{experiment_id}"]["state"]["experiment"][
        "window"
    ]["ends_at"]
    return {
        "evaluation_id": evaluation_id,
        "experiment_id": experiment_id,
        "experiment_revision": scenario.experiment_revision(experiment_id),
        "evaluator_principal_id": PRINCIPAL_IDS["evaluator"],
        "evaluator_run_id": stable_uuid("spoofed-evaluator-run"),
        "executor_principal_ids": [PRINCIPAL_IDS["specialist"]],
        "window_ends_at": window_ends_at,
        "criterion_results": [
            {
                "criterion_id": criterion_id,
                "outcome": outcome,
                "observed_value": observed_value,
                "evidence_ids": [] if evidence_ids is None else evidence_ids,
            }
        ],
        "result": result,
        "summary": summary,
    }


async def _submit(
    scenario: Scenario,
    payload: dict[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    return await scenario.service.invoke(
        "evaluation.submit",
        scenario.principals["evaluator"],
        mutation(
            "evaluation.submit",
            payload,
            expected_revision=scenario.experiment_revision(),
            key=key,
        ),
    )


@pytest.mark.asyncio
async def test_count_criterion_can_fail_with_empty_evidence_when_ledger_is_empty() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    closed = await scenario.close_experiment()
    experiment = closed["state"]["experiment"]
    assert experiment["state"] == "evaluating"

    response = await _submit(
        scenario,
        _zero_fail_payload(scenario),
        key="zero-evidence-fail",
    )
    evaluation = response["state"]["evaluation"]
    updated = response["state"]["experiment"]

    assert evaluation["result"] == "failed"
    assert evaluation["criterion_results"] == [
        {
            "criterion_id": "paid_pilot_requests",
            "outcome": "failed",
            "observed_value": 0,
            "evidence_ids": [],
        }
    ]
    assert updated["state"] == "failed"
    assert updated["evaluation_id"] == evaluation["evaluation_id"]
    assert updated["revision"] == experiment["revision"] + 1

    records = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "experiment"}},
    )
    shown = records["cell_records"]["records"][0]
    assert shown["state"]["state"] == "failed"
    assert shown["evaluation"]["result"] == "failed"
    assert shown["evaluation"]["criterion_results"][0]["evidence_ids"] == []


@pytest.mark.asyncio
async def test_activity_evidence_does_not_block_empty_count_failure() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    await scenario.claim_work()
    await scenario.append_evidence(
        evidence_type="cost",
        observation={"value": 1, "unit": "packet_sent", "summary": "One outbound packet."},
    )
    await scenario.complete_work()
    await scenario.close_experiment()

    response = await _submit(
        scenario,
        _zero_fail_payload(scenario, evaluation_id=stable_uuid("activity-zero-fail")),
        key="activity-does-not-count",
    )
    assert response["state"]["experiment"]["state"] == "failed"
    assert response["state"]["evaluation"]["criterion_results"][0]["evidence_ids"] == []


@pytest.mark.asyncio
async def test_empty_evidence_failure_is_refused_when_fitness_evidence_exists() -> None:
    scenario = Scenario()
    await scenario.build_evaluating_experiment()
    before = await scenario.store.export_company(COMPANY_ID)

    with pytest.raises(ValidationError, match="no qualifying fitness evidence"):
        await _submit(
            scenario,
            _zero_fail_payload(scenario, evaluation_id=stable_uuid("dishonest-zero-fail")),
            key="reject-hidden-fitness",
        )

    after = await scenario.store.export_company(COMPANY_ID)
    assert len(after["domain_events"]) == len(before["domain_events"])
    described = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {},
    )
    experiment = described["portfolio"]["cells"][0]["experiments"][0]
    assert experiment["state"] == "evaluating"


@pytest.mark.asyncio
async def test_empty_evidence_failure_rejects_nonzero_observed_value() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.close_experiment()

    with pytest.raises(ValidationError, match="observed_value"):
        await _submit(
            scenario,
            _zero_fail_payload(scenario, observed_value=1),
            key="reject-nonzero-empty-fail",
        )


@pytest.mark.asyncio
async def test_non_count_failure_still_requires_cited_evidence() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    starts_at = scenario.clock().isoformat().replace("+00:00", "Z")
    ends_at = (scenario.clock() + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    await scenario.invoke(
        "experiment.register",
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "cell_id": CELL_ID,
            "offer_version_id": MUTATED_OFFER_VERSION_ID,
            "hypothesis": "A rate metric still needs cited numeric evidence to fail.",
            "baseline": "No rate observed.",
            "treatment": "Measure a non-count unit.",
            "target_population": "One frozen ICP firm.",
            "metrics": [
                {
                    "metric_id": "paid_pilot_requests",
                    "name": "Contribution rate",
                    "unit": "basis_points",
                    "direction": "increase",
                }
            ],
            "success_thresholds": [
                {"metric_id": "paid_pilot_requests", "operator": "gte", "value": 1}
            ],
            "failure_thresholds": [
                {"metric_id": "paid_pilot_requests", "operator": "eq", "value": 0}
            ],
            "stop_rules": [
                {
                    "rule_id": "budget_exhausted",
                    "condition": "Actual cost reaches the fixed experiment budget.",
                    "effect": "pause_external_actions",
                }
            ],
            "window": {"starts_at": starts_at, "ends_at": ends_at},
            "budget": {"currency": "USD", "amount_minor": 10_000},
            "permitted_actions": ["evidence.append"],
            "executor_principal_ids": [PRINCIPAL_IDS["specialist"]],
            "evaluator_principal_id": PRINCIPAL_IDS["evaluator"],
        },
        expected_revision=0,
        key="register-non-count",
    )
    await scenario.start_experiment(expected_revision=1, key="start-non-count")
    await scenario.close_experiment()

    with pytest.raises(ValidationError, match="no numeric cited evidence"):
        await _submit(
            scenario,
            _zero_fail_payload(scenario),
            key="reject-non-count-empty",
        )


@pytest.mark.asyncio
async def test_protocol_describe_advertises_zero_evidence_failure() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["evaluator"],
        {},
    )
    evaluation = described["bet_workflow"]["evaluation"]
    rule = evaluation["zero_evidence_failure"]
    assert rule["metric_unit"] == "count"
    assert rule["outcome"] == "failed"
    assert rule["observed_value"] == 0
    assert rule["evidence_ids"] == []
    assert rule["requires"] == "no_qualifying_fitness_evidence"
    assert rule["passed_still_requires_evidence"] is True
    assert any("empty evidence_ids" in step for step in described["bet_workflow"]["operate"])

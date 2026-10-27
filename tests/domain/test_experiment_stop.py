"""Deterministic stop-rule evaluation used by describe projections and the tick."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_nature_company.errors import ValidationError
from ai_nature_company.experiment_stop import (
    BUDGET_EXHAUSTED,
    POLICY_VIOLATION,
    WINDOW_ELAPSED,
    cell_operating_eligibility,
    experiment_stop_assessment,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _experiment(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "state": "running",
        "window": {
            "starts_at": NOW.isoformat(),
            "ends_at": (NOW + timedelta(days=7)).isoformat(),
        },
        "budget": {"unit": "money", "currency": "USD", "amount_minor": 10_000},
        "stop_rules": [
            {
                "rule_id": BUDGET_EXHAUSTED,
                "condition": "Actual cost reaches the fixed experiment budget.",
                "effect": "pause_external_actions",
            },
            {
                "rule_id": POLICY_VIOLATION,
                "condition": "Verified evidence records a policy violation.",
                "effect": "cancel_experiment",
            },
        ],
    }
    payload.update(changes)
    return payload


def test_running_experiment_inside_window_is_clear() -> None:
    assessment = experiment_stop_assessment(_experiment(), now=NOW, actual_cost_minor=1_000)

    assert assessment["stop_state"] == "clear"
    assert assessment["close_eligible"] is False
    assert assessment["remaining_budget_minor"] == 9_000
    assert assessment["fired_rules"] == []


def test_elapsed_window_makes_a_running_experiment_close_eligible() -> None:
    assessment = experiment_stop_assessment(
        _experiment(),
        now=NOW + timedelta(days=7),
        actual_cost_minor=0,
    )

    assert assessment["stop_state"] == "fired"
    assert assessment["close_eligible"] is True
    assert assessment["fired_rules"][0]["rule_id"] == WINDOW_ELAPSED


def test_exhausted_budget_fires_without_using_activity_counts() -> None:
    assessment = experiment_stop_assessment(
        _experiment(),
        now=NOW,
        actual_cost_minor=10_000,
    )

    assert assessment["close_eligible"] is True
    assert {item["rule_id"] for item in assessment["fired_rules"]} == {BUDGET_EXHAUSTED}
    assert "packets_sent" not in assessment
    assert "activity" not in assessment


def test_policy_violation_evidence_fires_only_when_that_rule_is_frozen() -> None:
    fired = experiment_stop_assessment(
        _experiment(),
        now=NOW,
        evidence_types=("policy_violation",),
    )
    ignored = experiment_stop_assessment(
        _experiment(
            stop_rules=[
                {
                    "rule_id": BUDGET_EXHAUSTED,
                    "condition": "Budget only.",
                    "effect": "pause_external_actions",
                }
            ]
        ),
        now=NOW,
        evidence_types=("policy_violation",),
    )

    assert fired["close_eligible"] is True
    assert fired["fired_rules"][0]["rule_id"] == POLICY_VIOLATION
    assert ignored["close_eligible"] is False


def test_already_closed_experiment_is_not_close_eligible() -> None:
    assessment = experiment_stop_assessment(
        _experiment(state="evaluating"),
        now=NOW + timedelta(days=8),
    )

    assert assessment["stop_state"] == "fired"
    assert assessment["close_eligible"] is False


def test_cell_eligibility_keeps_kill_founder_gated() -> None:
    eligibility = cell_operating_eligibility(
        cell_status="active",
        experiment_assessments=({"close_eligible": True, "experiment_state": "running"},),
    )

    assert eligibility["pause_eligible"] is True
    assert eligibility["kill_eligible"] is True
    assert eligibility["kill_requires_founder"] is True
    assert eligibility["kill_owner_signed_requires"] == "founder_decision_ref"
    assert eligibility["close_eligible"] is True


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        experiment_stop_assessment(_experiment(), now=datetime(2026, 8, 31, 12, 0))

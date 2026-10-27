"""Deterministic experiment stop-rule evaluation.

This module applies already-frozen window, budget, and named stop rules.  It does
not interpret free-text conditions, invoke a model, or change strategy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from .errors import ValidationError

WINDOW_ELAPSED = "window_elapsed"
BUDGET_EXHAUSTED = "budget_exhausted"
POLICY_VIOLATION = "policy_violation"


def parse_aware_datetime(value: object, field: str = "timestamp") -> datetime:
    """Parse a stored ISO-8601 timestamp into an aware UTC datetime."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def experiment_stop_assessment(
    experiment: Mapping[str, Any],
    *,
    now: datetime,
    actual_cost_minor: int = 0,
    evidence_types: Iterable[str] = (),
) -> dict[str, Any]:
    """Return stop state, fired rules, and close eligibility for one experiment."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValidationError("tick time must be timezone-aware")
    if isinstance(actual_cost_minor, bool) or not isinstance(actual_cost_minor, int):
        raise ValidationError("actual_cost_minor must be an integer")
    if actual_cost_minor < 0:
        raise ValidationError("actual_cost_minor must not be negative")

    window = experiment.get("window")
    if not isinstance(window, Mapping):
        raise ValidationError("experiment window is required")
    ends_at = parse_aware_datetime(window.get("ends_at"), "window.ends_at")
    budget = experiment.get("budget")
    if not isinstance(budget, Mapping):
        raise ValidationError("experiment budget is required")
    try:
        budget_minor = int(budget["amount_minor"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("experiment budget amount_minor is required") from exc

    named_rules = {
        str(rule.get("rule_id"))
        for rule in experiment.get("stop_rules", ())
        if isinstance(rule, Mapping) and rule.get("rule_id")
    }
    adverse = {str(item) for item in evidence_types}
    candidates = (
        (
            WINDOW_ELAPSED,
            "evaluation_window_ended",
            now >= ends_at,
            True,
        ),
        (
            BUDGET_EXHAUSTED,
            "preregistered_budget_consumed",
            actual_cost_minor >= budget_minor,
            True,
        ),
        (
            POLICY_VIOLATION,
            "policy_violation_evidence",
            "policy_violation" in adverse,
            POLICY_VIOLATION in named_rules or not named_rules,
        ),
    )
    fired = [
        {"rule_id": rule_id, "reason": reason}
        for rule_id, reason, observed, applicable in candidates
        if observed and applicable
    ]
    status = str(experiment.get("state") or experiment.get("status") or "")
    snapshot: dict[str, Any] = {
        "stop_state": "fired" if fired else "clear",
        "fired_rules": fired,
        "close_eligible": status == "running" and bool(fired),
        "window_ends_at": ends_at.isoformat().replace("+00:00", "Z"),
        "budget_amount_minor": budget_minor,
        "actual_cost_minor": actual_cost_minor,
        "remaining_budget_minor": max(0, budget_minor - actual_cost_minor),
        "unit": budget.get("unit", "money"),
    }
    if budget.get("currency") is not None:
        snapshot["currency"] = budget["currency"]
    return snapshot


def cell_operating_eligibility(
    *,
    cell_status: str,
    experiment_assessments: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return pause/kill/close eligibility without treating activity as fitness."""

    assessments = list(experiment_assessments)
    return {
        "pause_eligible": cell_status == "active",
        "kill_eligible": cell_status in {"active", "paused"},
        "kill_requires_founder": True,
        "kill_owner_signed_requires": "founder_decision_ref",
        "close_eligible": any(item.get("close_eligible") for item in assessments),
        "open_experiment_count": sum(
            1
            for item in assessments
            if item.get("experiment_state") in {"registered", "running", "evaluating"}
        ),
    }


__all__ = [
    "BUDGET_EXHAUSTED",
    "POLICY_VIOLATION",
    "WINDOW_ELAPSED",
    "cell_operating_eligibility",
    "experiment_stop_assessment",
    "parse_aware_datetime",
]

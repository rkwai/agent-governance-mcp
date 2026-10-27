"""Deterministic ``fitness-v1`` gate, ranking, and digest utilities.

This module deliberately accepts only raw observations and ranking inputs.  A caller
cannot supply an authoritative score: numerators and denominators are always
recomputed by the substrate from the versioned formulas.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from functools import cmp_to_key
from typing import Any
from uuid import UUID

FITNESS_RULE_VERSION = "fitness-v1"
EXPLOITATION_FORMULA = "exploitation-v1"
EXPLORATION_FORMULA = "exploration-v1"

FITNESS_V1_GATE_ORDER: tuple[str, ...] = (
    "legality_and_policy",
    "truthfulness_and_claim_support",
    "customer_outcome_floor",
    "security_and_privacy",
    "budget_and_cash",
    "delivery_quality_floor",
    "data_sufficiency_and_independence",
)

_RELAXABLE_EXPLORATION_GATES = frozenset({"customer_outcome_floor", "delivery_quality_floor"})


def _integer(name: str, value: object, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _basis_points(name: str, value: object) -> int:
    result = _integer(name, value, minimum=0)
    if result > 10_000:
        raise ValueError(f"{name} must not exceed 10000")
    return result


def _boolean(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class FitnessThresholds:
    """Constitution thresholds used by all seven ``fitness-v1`` gates."""

    minimum_evidence_count: int
    minimum_customer_outcome_bps: int
    minimum_delivery_quality_bps: int
    company_limit_minor: int
    cell_limit_minor: int
    experiment_limit_minor: int
    single_action_limit_minor: int

    def __post_init__(self) -> None:
        _integer("minimum_evidence_count", self.minimum_evidence_count, minimum=1)
        _basis_points("minimum_customer_outcome_bps", self.minimum_customer_outcome_bps)
        _basis_points("minimum_delivery_quality_bps", self.minimum_delivery_quality_bps)
        _integer("company_limit_minor", self.company_limit_minor, minimum=0)
        _integer("cell_limit_minor", self.cell_limit_minor, minimum=0)
        _integer("experiment_limit_minor", self.experiment_limit_minor, minimum=0)
        _integer("single_action_limit_minor", self.single_action_limit_minor, minimum=0)

        if self.cell_limit_minor > self.company_limit_minor:
            raise ValueError("cell_limit_minor cannot exceed company_limit_minor")
        if self.experiment_limit_minor > self.cell_limit_minor:
            raise ValueError("experiment_limit_minor cannot exceed cell_limit_minor")
        if self.single_action_limit_minor > self.experiment_limit_minor:
            raise ValueError("single_action_limit_minor cannot exceed experiment_limit_minor")


@dataclass(frozen=True, slots=True)
class GateObservations:
    """Server-derived observations used to evaluate eligibility.

    ``*_spend_minor`` values are the amounts already spent before the requested
    allocation.  The budget gate checks each current spend plus the request.
    """

    open_legal_or_policy_block: bool
    all_material_promises_supported: bool
    unresolved_unsupported_claim_event: bool
    verified_customer_outcome_bps: int | None
    mandatory_harm_threshold_fired: bool
    unresolved_security_or_privacy_incident: bool
    unauthorized_data_scope: bool
    company_spend_minor: int
    cell_spend_minor: int
    experiment_spend_minor: int
    stage_available_minor: int
    requested_allocation_minor: int
    verified_delivery_quality_bps: int | None
    delivery_overrun_stop_rule_fired: bool
    evidence_count: int
    sources_valid: bool
    lineage_valid: bool
    evaluation_independent: bool

    def __post_init__(self) -> None:
        boolean_fields = (
            "open_legal_or_policy_block",
            "all_material_promises_supported",
            "unresolved_unsupported_claim_event",
            "mandatory_harm_threshold_fired",
            "unresolved_security_or_privacy_incident",
            "unauthorized_data_scope",
            "delivery_overrun_stop_rule_fired",
            "sources_valid",
            "lineage_valid",
            "evaluation_independent",
        )
        for name in boolean_fields:
            _boolean(name, getattr(self, name))

        if self.verified_customer_outcome_bps is not None:
            _basis_points("verified_customer_outcome_bps", self.verified_customer_outcome_bps)
        if self.verified_delivery_quality_bps is not None:
            _basis_points("verified_delivery_quality_bps", self.verified_delivery_quality_bps)

        for name in (
            "company_spend_minor",
            "cell_spend_minor",
            "experiment_spend_minor",
            "stage_available_minor",
            "requested_allocation_minor",
            "evidence_count",
        ):
            _integer(name, getattr(self, name), minimum=0)


def _canonical_evidence_ids(values: Iterable[UUID | str]) -> list[str]:
    try:
        identifiers = {str(UUID(str(value))) for value in values}
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("evidence IDs must be UUIDs") from exc
    return sorted(identifiers)


def evaluate_fitness_gates(
    observations: GateObservations,
    thresholds: FitnessThresholds,
    evidence_ids_by_gate: Mapping[str, Iterable[UUID | str]] | None = None,
) -> list[dict[str, object]]:
    """Compute all seven gate results in their normative order.

    Missing evidence annotations produce an empty ``evidence_ids`` array; they do
    not change a gate's deterministic pass condition.
    """

    evidence_ids_by_gate = evidence_ids_by_gate or {}
    unknown_gates = set(evidence_ids_by_gate).difference(FITNESS_V1_GATE_ORDER)
    if unknown_gates:
        names = ", ".join(sorted(unknown_gates))
        raise ValueError(f"unknown fitness gate evidence: {names}")

    requested = observations.requested_allocation_minor
    budget_passed = (
        observations.company_spend_minor + requested <= thresholds.company_limit_minor
        and observations.cell_spend_minor + requested <= thresholds.cell_limit_minor
        and observations.experiment_spend_minor + requested <= thresholds.experiment_limit_minor
        and requested <= observations.stage_available_minor
        and requested <= thresholds.single_action_limit_minor
    )

    passed_by_gate = {
        "legality_and_policy": not observations.open_legal_or_policy_block,
        "truthfulness_and_claim_support": (
            observations.all_material_promises_supported
            and not observations.unresolved_unsupported_claim_event
        ),
        "customer_outcome_floor": (
            observations.verified_customer_outcome_bps is not None
            and observations.verified_customer_outcome_bps
            >= thresholds.minimum_customer_outcome_bps
            and not observations.mandatory_harm_threshold_fired
        ),
        "security_and_privacy": (
            not observations.unresolved_security_or_privacy_incident
            and not observations.unauthorized_data_scope
        ),
        "budget_and_cash": budget_passed,
        "delivery_quality_floor": (
            observations.verified_delivery_quality_bps is not None
            and observations.verified_delivery_quality_bps
            >= thresholds.minimum_delivery_quality_bps
            and not observations.delivery_overrun_stop_rule_fired
        ),
        "data_sufficiency_and_independence": (
            observations.evidence_count >= thresholds.minimum_evidence_count
            and observations.sources_valid
            and observations.lineage_valid
            and observations.evaluation_independent
        ),
    }

    return [
        {
            "gate": gate,
            "passed": passed_by_gate[gate],
            "evidence_ids": _canonical_evidence_ids(evidence_ids_by_gate.get(gate, ())),
        }
        for gate in FITNESS_V1_GATE_ORDER
    ]


def gates_are_eligible(
    gate_results: Sequence[Mapping[str, object]],
    stage: str,
    *,
    permit_uncertain_customer_outcome: bool = False,
    permit_uncertain_delivery_quality: bool = False,
    containment_basis_verified: bool = False,
) -> bool:
    """Return whether a complete gate set permits ranking in ``stage``.

    Missing, duplicate, extra, or malformed results fail closed. Exploration can
    relax only the two uncertainty gates explicitly identified by the constitution;
    every safety, budget, and independence gate remains mandatory. Containment is
    eligible only when the application has verified and recorded an independent
    policy, stop-rule, or founder-authorized basis.
    """

    if stage not in {"exploitation", "exploration", "containment"}:
        raise ValueError("stage must be exploitation, exploration, or containment")
    if not isinstance(containment_basis_verified, bool):
        raise ValueError("containment_basis_verified must be a boolean")
    if len(gate_results) != len(FITNESS_V1_GATE_ORDER):
        return False

    results: dict[str, bool] = {}
    for result in gate_results:
        gate = result.get("gate")
        passed = result.get("passed")
        if gate not in FITNESS_V1_GATE_ORDER or gate in results or not isinstance(passed, bool):
            return False
        results[str(gate)] = passed
    if set(results) != set(FITNESS_V1_GATE_ORDER):
        return False

    if stage == "containment":
        return containment_basis_verified

    permitted_failures: set[str] = set()
    if stage == "exploration":
        if permit_uncertain_customer_outcome:
            permitted_failures.add("customer_outcome_floor")
        if permit_uncertain_delivery_quality:
            permitted_failures.add("delivery_quality_floor")
        if not permitted_failures.issubset(_RELAXABLE_EXPLORATION_GATES):
            return False
    return all(passed or gate in permitted_failures for gate, passed in results.items())


@dataclass(frozen=True, slots=True)
class ExploitationInputs:
    expected_12m_contribution_profit_minor: int
    evidence_confidence_bps: int
    expected_repeatability_bps: int
    capital_at_risk_minor: int
    time_to_cash_days: int

    def __post_init__(self) -> None:
        _integer(
            "expected_12m_contribution_profit_minor",
            self.expected_12m_contribution_profit_minor,
        )
        _basis_points("evidence_confidence_bps", self.evidence_confidence_bps)
        _basis_points("expected_repeatability_bps", self.expected_repeatability_bps)
        _integer("capital_at_risk_minor", self.capital_at_risk_minor, minimum=0)
        _integer("time_to_cash_days", self.time_to_cash_days, minimum=0)

    def snapshot(self) -> dict[str, int]:
        return {
            "expected_12m_contribution_profit_minor": (self.expected_12m_contribution_profit_minor),
            "evidence_confidence_bps": self.evidence_confidence_bps,
            "expected_repeatability_bps": self.expected_repeatability_bps,
            "capital_at_risk_minor": self.capital_at_risk_minor,
            "time_to_cash_days": self.time_to_cash_days,
        }


@dataclass(frozen=True, slots=True)
class ExplorationInputs:
    expected_decision_value_minor: int
    resolution_probability_bps: int
    experiment_cost_minor: int
    experiment_duration_days: int

    def __post_init__(self) -> None:
        _integer("expected_decision_value_minor", self.expected_decision_value_minor, minimum=0)
        _basis_points("resolution_probability_bps", self.resolution_probability_bps)
        _integer("experiment_cost_minor", self.experiment_cost_minor, minimum=0)
        _integer("experiment_duration_days", self.experiment_duration_days, minimum=1)

    def snapshot(self) -> dict[str, int]:
        return {
            "expected_decision_value_minor": self.expected_decision_value_minor,
            "resolution_probability_bps": self.resolution_probability_bps,
            "experiment_cost_minor": self.experiment_cost_minor,
            "experiment_duration_days": self.experiment_duration_days,
        }


def _input_snapshot(payload: Mapping[str, object]) -> Mapping[str, object]:
    nested = payload.get("input_snapshot")
    if nested is None:
        return payload
    if not isinstance(nested, Mapping):
        raise ValueError("input_snapshot must be an object")
    return nested


def _exploitation_inputs(
    inputs: ExploitationInputs | Mapping[str, object],
) -> ExploitationInputs:
    if isinstance(inputs, ExploitationInputs):
        return inputs
    snapshot = _input_snapshot(inputs)
    try:
        return ExploitationInputs(
            expected_12m_contribution_profit_minor=snapshot[
                "expected_12m_contribution_profit_minor"
            ],
            evidence_confidence_bps=snapshot["evidence_confidence_bps"],
            expected_repeatability_bps=snapshot["expected_repeatability_bps"],
            capital_at_risk_minor=snapshot["capital_at_risk_minor"],
            time_to_cash_days=snapshot["time_to_cash_days"],
        )
    except KeyError as exc:
        raise ValueError(f"missing exploitation input: {exc.args[0]}") from exc


def _exploration_inputs(
    inputs: ExplorationInputs | Mapping[str, object],
) -> ExplorationInputs:
    if isinstance(inputs, ExplorationInputs):
        return inputs
    snapshot = _input_snapshot(inputs)
    try:
        return ExplorationInputs(
            expected_decision_value_minor=snapshot["expected_decision_value_minor"],
            resolution_probability_bps=snapshot["resolution_probability_bps"],
            experiment_cost_minor=snapshot["experiment_cost_minor"],
            experiment_duration_days=snapshot["experiment_duration_days"],
        )
    except KeyError as exc:
        raise ValueError(f"missing exploration input: {exc.args[0]}") from exc


def compute_exploitation_ranking(
    inputs: ExploitationInputs | Mapping[str, object],
) -> dict[str, object]:
    """Recompute the exact raw ``exploitation-v1`` fraction from raw inputs."""

    values = _exploitation_inputs(inputs)
    numerator = (
        values.expected_12m_contribution_profit_minor
        * values.evidence_confidence_bps
        * values.expected_repeatability_bps
        * 30
    )
    denominator = (
        max(values.capital_at_risk_minor, 1) * max(values.time_to_cash_days, 30) * 10_000 * 10_000
    )
    return {
        "formula": EXPLOITATION_FORMULA,
        "score_numerator": numerator,
        "score_denominator": denominator,
        "input_snapshot": values.snapshot(),
    }


def compute_exploration_ranking(
    inputs: ExplorationInputs | Mapping[str, object],
) -> dict[str, object]:
    """Recompute the exact raw ``exploration-v1`` fraction from raw inputs."""

    values = _exploration_inputs(inputs)
    numerator = values.expected_decision_value_minor * values.resolution_probability_bps
    denominator = (
        max(values.experiment_cost_minor, 1) * max(values.experiment_duration_days, 1) * 10_000
    )
    return {
        "formula": EXPLORATION_FORMULA,
        "score_numerator": numerator,
        "score_denominator": denominator,
        "input_snapshot": values.snapshot(),
    }


def containment_ranking() -> dict[str, object]:
    """Return the only valid ranking value for a containment decision."""

    return {
        "formula": "not-ranked",
        "score_numerator": 0,
        "score_denominator": 1,
        "input_snapshot": {},
    }


def compare_rational(
    left_numerator: int,
    left_denominator: int,
    right_numerator: int,
    right_denominator: int,
) -> int:
    """Compare two exact fractions by integer cross multiplication."""

    left_numerator = _integer("left_numerator", left_numerator)
    right_numerator = _integer("right_numerator", right_numerator)
    left_denominator = _integer("left_denominator", left_denominator, minimum=1)
    right_denominator = _integer("right_denominator", right_denominator, minimum=1)
    left = left_numerator * right_denominator
    right = right_numerator * left_denominator
    return (left > right) - (left < right)


def _aware_utc(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ExploitationCandidate:
    target_id: UUID
    proposed_at: datetime
    inputs: ExploitationInputs

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", UUID(str(self.target_id)))
        object.__setattr__(self, "proposed_at", _aware_utc("proposed_at", self.proposed_at))
        if not isinstance(self.inputs, ExploitationInputs):
            raise ValueError("inputs must be ExploitationInputs")


@dataclass(frozen=True, slots=True)
class ExplorationCandidate:
    target_id: UUID
    proposed_at: datetime
    inputs: ExplorationInputs

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", UUID(str(self.target_id)))
        object.__setattr__(self, "proposed_at", _aware_utc("proposed_at", self.proposed_at))
        if not isinstance(self.inputs, ExplorationInputs):
            raise ValueError("inputs must be ExplorationInputs")


def _compare_exploitation_candidates(
    left: ExploitationCandidate, right: ExploitationCandidate
) -> int:
    left_rank = compute_exploitation_ranking(left.inputs)
    right_rank = compute_exploitation_ranking(right.inputs)
    score_order = compare_rational(
        left_rank["score_numerator"],
        left_rank["score_denominator"],
        right_rank["score_numerator"],
        right_rank["score_denominator"],
    )
    if score_order:
        return -score_order
    tie_breaks = (
        (left.inputs.capital_at_risk_minor, right.inputs.capital_at_risk_minor),
        (left.inputs.time_to_cash_days, right.inputs.time_to_cash_days),
        (left.proposed_at, right.proposed_at),
        (str(left.target_id), str(right.target_id)),
    )
    for left_value, right_value in tie_breaks:
        if left_value != right_value:
            return -1 if left_value < right_value else 1
    return 0


def _compare_exploration_candidates(left: ExplorationCandidate, right: ExplorationCandidate) -> int:
    left_rank = compute_exploration_ranking(left.inputs)
    right_rank = compute_exploration_ranking(right.inputs)
    score_order = compare_rational(
        left_rank["score_numerator"],
        left_rank["score_denominator"],
        right_rank["score_numerator"],
        right_rank["score_denominator"],
    )
    if score_order:
        return -score_order
    tie_breaks = (
        (left.inputs.experiment_cost_minor, right.inputs.experiment_cost_minor),
        (left.inputs.experiment_duration_days, right.inputs.experiment_duration_days),
        (left.proposed_at, right.proposed_at),
        (str(left.target_id), str(right.target_id)),
    )
    for left_value, right_value in tie_breaks:
        if left_value != right_value:
            return -1 if left_value < right_value else 1
    return 0


def rank_exploitation_candidates(
    candidates: Iterable[ExploitationCandidate],
) -> tuple[ExploitationCandidate, ...]:
    """Order an exploitation pool by exact score and normative tie-breaks."""

    values = tuple(candidates)
    if not all(isinstance(candidate, ExploitationCandidate) for candidate in values):
        raise ValueError("all candidates must be ExploitationCandidate values")
    return tuple(sorted(values, key=cmp_to_key(_compare_exploitation_candidates)))


def rank_exploration_candidates(
    candidates: Iterable[ExplorationCandidate],
) -> tuple[ExplorationCandidate, ...]:
    """Order an exploration pool by exact score and normative tie-breaks."""

    values = tuple(candidates)
    if not all(isinstance(candidate, ExplorationCandidate) for candidate in values):
        raise ValueError("all candidates must be ExplorationCandidate values")
    return tuple(sorted(values, key=cmp_to_key(_compare_exploration_candidates)))


def _canonical_value(value: object) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError("canonical decision inputs must not contain floating-point values")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        moment = _aware_utc("decision timestamp", value)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical decision object keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=_canonical_sort_key)
    raise ValueError(f"unsupported canonical decision value: {type(value).__name__}")


def _canonical_sort_key(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_decision_bytes(decision_input: Mapping[str, object]) -> bytes:
    """Serialize decision input with stable keys, separators, and typed values."""

    if not isinstance(decision_input, Mapping):
        raise ValueError("decision input must be an object")
    canonical = _canonical_value(decision_input)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def canonical_decision_digest(decision_input: Mapping[str, object]) -> str:
    """Return the lowercase SHA-256 digest of canonical decision input."""

    return hashlib.sha256(canonical_decision_bytes(decision_input)).hexdigest()


__all__ = [
    "EXPLOITATION_FORMULA",
    "EXPLORATION_FORMULA",
    "FITNESS_RULE_VERSION",
    "FITNESS_V1_GATE_ORDER",
    "ExploitationCandidate",
    "ExploitationInputs",
    "ExplorationCandidate",
    "ExplorationInputs",
    "FitnessThresholds",
    "GateObservations",
    "canonical_decision_bytes",
    "canonical_decision_digest",
    "compare_rational",
    "compute_exploitation_ranking",
    "compute_exploration_ranking",
    "containment_ranking",
    "evaluate_fitness_gates",
    "gates_are_eligible",
    "rank_exploitation_candidates",
    "rank_exploration_candidates",
]

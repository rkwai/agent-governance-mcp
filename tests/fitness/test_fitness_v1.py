from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

import pytest

from ai_nature_company.fitness import (
    FITNESS_V1_GATE_ORDER,
    ExploitationCandidate,
    ExploitationInputs,
    ExplorationCandidate,
    ExplorationInputs,
    FitnessThresholds,
    GateObservations,
    canonical_decision_bytes,
    canonical_decision_digest,
    compare_rational,
    compute_exploitation_ranking,
    compute_exploration_ranking,
    containment_ranking,
    evaluate_fitness_gates,
    gates_are_eligible,
    rank_exploitation_candidates,
    rank_exploration_candidates,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


def uuid_for(number: int) -> UUID:
    return UUID(int=number)


@pytest.fixture
def thresholds() -> FitnessThresholds:
    return FitnessThresholds(
        minimum_evidence_count=3,
        minimum_customer_outcome_bps=8_000,
        minimum_delivery_quality_bps=9_000,
        company_limit_minor=100_000,
        cell_limit_minor=50_000,
        experiment_limit_minor=20_000,
        single_action_limit_minor=10_000,
    )


@pytest.fixture
def passing_observations() -> GateObservations:
    return GateObservations(
        open_legal_or_policy_block=False,
        all_material_promises_supported=True,
        unresolved_unsupported_claim_event=False,
        verified_customer_outcome_bps=8_500,
        mandatory_harm_threshold_fired=False,
        unresolved_security_or_privacy_incident=False,
        unauthorized_data_scope=False,
        company_spend_minor=50_000,
        cell_spend_minor=20_000,
        experiment_spend_minor=5_000,
        stage_available_minor=20_000,
        requested_allocation_minor=10_000,
        verified_delivery_quality_bps=9_500,
        delivery_overrun_stop_rule_fired=False,
        evidence_count=3,
        sources_valid=True,
        lineage_valid=True,
        evaluation_independent=True,
    )


def test_all_seven_gates_are_computed_in_normative_order(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
) -> None:
    evidence = {
        gate: [uuid_for(index + 10), uuid_for(index), uuid_for(index)]
        for index, gate in enumerate(FITNESS_V1_GATE_ORDER, start=1)
    }

    results = evaluate_fitness_gates(passing_observations, thresholds, evidence)

    assert [result["gate"] for result in results] == list(FITNESS_V1_GATE_ORDER)
    assert all(result["passed"] is True for result in results)
    assert results[0]["evidence_ids"] == [str(uuid_for(1)), str(uuid_for(11))]
    assert gates_are_eligible(results, "exploitation")


@pytest.mark.parametrize(
    ("changes", "failed_gate"),
    [
        ({"open_legal_or_policy_block": True}, "legality_and_policy"),
        ({"all_material_promises_supported": False}, "truthfulness_and_claim_support"),
        ({"unresolved_unsupported_claim_event": True}, "truthfulness_and_claim_support"),
        ({"verified_customer_outcome_bps": 7_999}, "customer_outcome_floor"),
        ({"mandatory_harm_threshold_fired": True}, "customer_outcome_floor"),
        (
            {"unresolved_security_or_privacy_incident": True},
            "security_and_privacy",
        ),
        ({"unauthorized_data_scope": True}, "security_and_privacy"),
        ({"requested_allocation_minor": 10_001}, "budget_and_cash"),
        ({"stage_available_minor": 9_999}, "budget_and_cash"),
        ({"verified_delivery_quality_bps": 8_999}, "delivery_quality_floor"),
        ({"delivery_overrun_stop_rule_fired": True}, "delivery_quality_floor"),
        ({"evidence_count": 2}, "data_sufficiency_and_independence"),
        ({"sources_valid": False}, "data_sufficiency_and_independence"),
        ({"lineage_valid": False}, "data_sufficiency_and_independence"),
        ({"evaluation_independent": False}, "data_sufficiency_and_independence"),
    ],
)
def test_each_gate_fails_closed_from_explicit_observation(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
    changes: dict[str, object],
    failed_gate: str,
) -> None:
    results = evaluate_fitness_gates(replace(passing_observations, **changes), thresholds)

    failed = [result["gate"] for result in results if not result["passed"]]
    assert failed == [failed_gate]
    assert not gates_are_eligible(results, "exploitation")


@pytest.mark.parametrize(
    "field",
    ["verified_customer_outcome_bps", "verified_delivery_quality_bps"],
)
def test_missing_measured_floor_fails_closed(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
    field: str,
) -> None:
    results = evaluate_fitness_gates(replace(passing_observations, **{field: None}), thresholds)

    assert not gates_are_eligible(results, "exploitation")


def test_budget_gate_includes_existing_spend_plus_request(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
) -> None:
    observations = replace(
        passing_observations,
        experiment_spend_minor=10_001,
        requested_allocation_minor=10_000,
    )

    results = evaluate_fitness_gates(observations, thresholds)

    budget = next(result for result in results if result["gate"] == "budget_and_cash")
    assert budget["passed"] is False


def test_exploration_relaxes_only_explicit_outcome_and_delivery_uncertainty(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
) -> None:
    uncertain = replace(
        passing_observations,
        verified_customer_outcome_bps=None,
        verified_delivery_quality_bps=None,
    )
    results = evaluate_fitness_gates(uncertain, thresholds)

    assert not gates_are_eligible(results, "exploration")
    assert not gates_are_eligible(
        results,
        "exploration",
        permit_uncertain_customer_outcome=True,
    )
    assert gates_are_eligible(
        results,
        "exploration",
        permit_uncertain_customer_outcome=True,
        permit_uncertain_delivery_quality=True,
    )

    unsafe = evaluate_fitness_gates(
        replace(uncertain, unresolved_security_or_privacy_incident=True), thresholds
    )
    assert not gates_are_eligible(
        unsafe,
        "exploration",
        permit_uncertain_customer_outcome=True,
        permit_uncertain_delivery_quality=True,
    )


def test_missing_duplicate_extra_and_malformed_gate_sets_fail_closed(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
) -> None:
    results = evaluate_fitness_gates(passing_observations, thresholds)

    assert not gates_are_eligible(results[:-1], "exploitation")
    assert not gates_are_eligible([*results[:-1], results[0]], "exploitation")
    assert not gates_are_eligible([*results, {"gate": "invented", "passed": True}], "exploitation")
    malformed = [*results[:-1], {"gate": FITNESS_V1_GATE_ORDER[-1], "passed": 1}]
    assert not gates_are_eligible(malformed, "exploitation")


def test_containment_requires_a_complete_gate_set_and_verified_recorded_basis(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
) -> None:
    results = evaluate_fitness_gates(
        replace(passing_observations, unresolved_security_or_privacy_incident=True),
        thresholds,
    )

    assert not gates_are_eligible(results, "containment")
    assert gates_are_eligible(results, "containment", containment_basis_verified=True)
    assert not gates_are_eligible(
        results[:-1],
        "containment",
        containment_basis_verified=True,
    )
    with pytest.raises(ValueError, match="containment_basis_verified"):
        gates_are_eligible(
            results,
            "containment",
            containment_basis_verified=1,  # type: ignore[arg-type]
        )


def test_unknown_gate_evidence_and_bad_threshold_hierarchy_are_rejected(
    passing_observations: GateObservations,
    thresholds: FitnessThresholds,
) -> None:
    with pytest.raises(ValueError, match="unknown fitness gate"):
        evaluate_fitness_gates(
            passing_observations,
            thresholds,
            {"made_up_gate": [uuid_for(1)]},
        )

    with pytest.raises(ValueError, match="cell_limit_minor"):
        replace(thresholds, cell_limit_minor=100_001)


def test_exploitation_formula_uses_exact_unreduced_integer_fraction() -> None:
    ranking = compute_exploitation_ranking(
        ExploitationInputs(
            expected_12m_contribution_profit_minor=500_000,
            evidence_confidence_bps=9_000,
            expected_repeatability_bps=6_000,
            capital_at_risk_minor=10_000,
            time_to_cash_days=30,
        )
    )

    assert ranking == {
        "formula": "exploitation-v1",
        "score_numerator": 810_000_000_000_000,
        "score_denominator": 30_000_000_000_000,
        "input_snapshot": {
            "expected_12m_contribution_profit_minor": 500_000,
            "evidence_confidence_bps": 9_000,
            "expected_repeatability_bps": 6_000,
            "capital_at_risk_minor": 10_000,
            "time_to_cash_days": 30,
        },
    }


def test_exploitation_uses_denominator_floors_and_preserves_negative_profit() -> None:
    ranking = compute_exploitation_ranking(
        {
            "expected_12m_contribution_profit_minor": -10,
            "evidence_confidence_bps": 10_000,
            "expected_repeatability_bps": 10_000,
            "capital_at_risk_minor": 0,
            "time_to_cash_days": 0,
        }
    )

    assert ranking["score_numerator"] == -30_000_000_000
    assert ranking["score_denominator"] == 3_000_000_000


def test_exploration_formula_uses_exact_unreduced_integer_fraction() -> None:
    ranking = compute_exploration_ranking(
        ExplorationInputs(
            expected_decision_value_minor=100_000,
            resolution_probability_bps=5_000,
            experiment_cost_minor=2_000,
            experiment_duration_days=14,
        )
    )

    assert ranking == {
        "formula": "exploration-v1",
        "score_numerator": 500_000_000,
        "score_denominator": 280_000_000,
        "input_snapshot": {
            "expected_decision_value_minor": 100_000,
            "resolution_probability_bps": 5_000,
            "experiment_cost_minor": 2_000,
            "experiment_duration_days": 14,
        },
    }


@pytest.mark.parametrize(
    ("computer", "payload", "expected_numerator", "expected_denominator"),
    [
        (
            compute_exploitation_ranking,
            {
                "formula": "exploration-v1",
                "score_numerator": 10**100,
                "score_denominator": 1,
                "input_snapshot": {
                    "expected_12m_contribution_profit_minor": 10,
                    "evidence_confidence_bps": 10_000,
                    "expected_repeatability_bps": 10_000,
                    "capital_at_risk_minor": 1,
                    "time_to_cash_days": 30,
                    "score_numerator": -(10**100),
                    "score_denominator": 0,
                },
            },
            30_000_000_000,
            3_000_000_000,
        ),
        (
            compute_exploration_ranking,
            {
                "formula": "exploitation-v1",
                "score_numerator": 10**100,
                "score_denominator": 1,
                "input_snapshot": {
                    "expected_decision_value_minor": 10,
                    "resolution_probability_bps": 10_000,
                    "experiment_cost_minor": 1,
                    "experiment_duration_days": 1,
                    "score_numerator": -(10**100),
                    "score_denominator": 0,
                },
            },
            100_000,
            10_000,
        ),
    ],
)
def test_caller_supplied_formula_and_score_fields_cannot_affect_result(
    computer: object,
    payload: dict[str, object],
    expected_numerator: int,
    expected_denominator: int,
) -> None:
    assert callable(computer)
    ranking = computer(payload)

    assert ranking["score_numerator"] == expected_numerator
    assert ranking["score_denominator"] == expected_denominator


def test_rational_comparison_uses_unbounded_cross_multiplication() -> None:
    huge = 10**400

    assert compare_rational(huge + 1, huge, 1, 1) == 1
    assert compare_rational(-1, 3, -2, 5) == 1
    assert compare_rational(2, 4, 1, 2) == 0
    with pytest.raises(ValueError, match="at least 1"):
        compare_rational(1, 0, 1, 1)


def exploitation_candidate(
    number: int,
    *,
    profit: int = 100,
    capital: int = 10,
    days: int = 30,
    proposed_at: datetime = NOW,
) -> ExploitationCandidate:
    return ExploitationCandidate(
        target_id=uuid_for(number),
        proposed_at=proposed_at,
        inputs=ExploitationInputs(
            expected_12m_contribution_profit_minor=profit,
            evidence_confidence_bps=10_000,
            expected_repeatability_bps=10_000,
            capital_at_risk_minor=capital,
            time_to_cash_days=days,
        ),
    )


def test_exploitation_ranking_applies_every_tie_break_in_order() -> None:
    assert rank_exploitation_candidates(
        [exploitation_candidate(1, profit=100), exploitation_candidate(2, profit=200)]
    )[0].target_id == uuid_for(2)

    assert rank_exploitation_candidates(
        [
            exploitation_candidate(1, profit=200, capital=20),
            exploitation_candidate(2, profit=100, capital=10),
        ]
    )[0].target_id == uuid_for(2)

    assert rank_exploitation_candidates(
        [exploitation_candidate(1, days=30), exploitation_candidate(2, days=10)]
    )[0].target_id == uuid_for(2)

    assert rank_exploitation_candidates(
        [
            exploitation_candidate(1, proposed_at=NOW + timedelta(seconds=1)),
            exploitation_candidate(2, proposed_at=NOW),
        ]
    )[0].target_id == uuid_for(2)

    assert rank_exploitation_candidates([exploitation_candidate(2), exploitation_candidate(1)])[
        0
    ].target_id == uuid_for(1)


def exploration_candidate(
    number: int,
    *,
    value: int = 100,
    cost: int = 10,
    days: int = 1,
    proposed_at: datetime = NOW,
) -> ExplorationCandidate:
    return ExplorationCandidate(
        target_id=uuid_for(number),
        proposed_at=proposed_at,
        inputs=ExplorationInputs(
            expected_decision_value_minor=value,
            resolution_probability_bps=10_000,
            experiment_cost_minor=cost,
            experiment_duration_days=days,
        ),
    )


def test_exploration_ranking_applies_every_tie_break_in_order() -> None:
    assert rank_exploration_candidates(
        [exploration_candidate(1, value=100), exploration_candidate(2, value=200)]
    )[0].target_id == uuid_for(2)

    assert rank_exploration_candidates(
        [
            exploration_candidate(1, value=100, cost=1),
            exploration_candidate(2, value=100, cost=0),
        ]
    )[0].target_id == uuid_for(2)

    assert rank_exploration_candidates(
        [
            exploration_candidate(1, value=200, days=2),
            exploration_candidate(2, value=100, days=1),
        ]
    )[0].target_id == uuid_for(2)

    assert rank_exploration_candidates(
        [
            exploration_candidate(1, proposed_at=NOW + timedelta(seconds=1)),
            exploration_candidate(2, proposed_at=NOW),
        ]
    )[0].target_id == uuid_for(2)

    assert rank_exploration_candidates([exploration_candidate(2), exploration_candidate(1)])[
        0
    ].target_id == uuid_for(1)


def test_containment_is_never_ranked() -> None:
    assert containment_ranking() == {
        "formula": "not-ranked",
        "score_numerator": 0,
        "score_denominator": 1,
        "input_snapshot": {},
    }


class ExampleValue(StrEnum):
    ACTIVE = "active"


def test_canonical_decision_digest_is_stable_across_order_and_typed_values() -> None:
    first = {
        "z": {"beta": 2, "alpha": 1},
        "target_id": uuid_for(42),
        "at": NOW,
        "gates": frozenset({"security_and_privacy", "budget_and_cash"}),
        "state": ExampleValue.ACTIVE,
    }
    second = {
        "state": "active",
        "gates": {"budget_and_cash", "security_and_privacy"},
        "at": "2026-08-29T12:00:00.000000Z",
        "target_id": str(uuid_for(42)),
        "z": {"alpha": 1, "beta": 2},
    }

    assert canonical_decision_bytes(first) == canonical_decision_bytes(second)
    assert canonical_decision_digest(first) == canonical_decision_digest(second)
    assert len(canonical_decision_digest(first)) == 64


def test_canonical_digest_has_known_sha256_vector_and_is_input_sensitive() -> None:
    assert canonical_decision_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert canonical_decision_digest({"b": 2, "a": 1}) == (
        "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    assert canonical_decision_digest({"a": 1}) != canonical_decision_digest({"a": 2})


@pytest.mark.parametrize(
    "decision_input",
    [
        {"score": 0.1},
        {1: "non-string key"},
        {"at": datetime(2026, 8, 29, 12)},
        {"unsupported": object()},
    ],
)
def test_canonical_digest_rejects_ambiguous_or_unsupported_values(
    decision_input: dict[object, object],
) -> None:
    with pytest.raises(ValueError):
        canonical_decision_digest(decision_input)


@pytest.mark.parametrize(
    "inputs",
    [
        {
            "expected_12m_contribution_profit_minor": 1,
            "evidence_confidence_bps": 10_001,
            "expected_repeatability_bps": 1,
            "capital_at_risk_minor": 1,
            "time_to_cash_days": 1,
        },
        {
            "expected_12m_contribution_profit_minor": 1,
            "evidence_confidence_bps": True,
            "expected_repeatability_bps": 1,
            "capital_at_risk_minor": 1,
            "time_to_cash_days": 1,
        },
    ],
)
def test_ranking_rejects_out_of_range_or_boolean_numeric_inputs(
    inputs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        compute_exploitation_ranking(inputs)

"""Contract tests for activity vs fitness evidence classification."""

from __future__ import annotations

from ai_nature_company.evidence_class import (
    SELECT_READS_EVIDENCE_CLASS,
    classify_evidence,
    metabolic_class,
)


def test_named_activity_units_are_activity_even_when_type_is_other() -> None:
    for unit in (
        "packet_sent",
        "packets_sent",
        "scout_ranked_drafts",
        "packet_not_sent",
        "packet_killed",
        "market_signal",
        "demand_hint",
        "observed_behavior",
        "world_signal",
    ):
        assert (
            classify_evidence({"type": "other", "observation": {"unit": unit, "value": 1}})
            == "activity"
        )


def test_customer_outcome_types_and_payable_units_are_fitness() -> None:
    assert classify_evidence({"type": "outcome", "observation": {"unit": "basis_points"}}) == (
        "fitness"
    )
    assert classify_evidence({"type": "reply", "observation": {"unit": "count"}}) == "fitness"
    assert (
        classify_evidence(
            {"type": "other", "observation": {"unit": "payable_covering_conversation"}}
        )
        == "fitness"
    )


def test_immune_and_unclassified_other_are_neither() -> None:
    assert classify_evidence({"type": "policy_violation", "observation": {"unit": "count"}}) is None
    assert classify_evidence({"type": "other", "observation": {"unit": "count"}}) is None


def test_transform_grammar_selects_metabolized_fitness_not_packets() -> None:
    packet = {"type": "other", "observation": {"unit": "packet_sent", "value": 1}}
    outcome = {"type": "outcome", "observation": {"unit": "basis_points"}}
    assert metabolic_class(packet) == "unmetabolized"
    assert metabolic_class(outcome) == "metabolized"
    assert SELECT_READS_EVIDENCE_CLASS == "fitness"
    assert classify_evidence(packet) != SELECT_READS_EVIDENCE_CLASS
    assert classify_evidence(outcome) == SELECT_READS_EVIDENCE_CLASS

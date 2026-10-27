"""Deterministic Transform grammar: activity vs fitness evidence.

Sense and Act write into the shared medium. Transform is the force that
metabolizes those writes into structured evidence or economics. Activity
counts are unmetabolized motion (packets, drafts, Sense units). Fitness
counts are metabolized customer-outcome or payable classes. Select reads
fitness, not packets. Classification never invokes a model and does not
change fitness rank.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

EvidenceClass = Literal["activity", "fitness"]
MetabolicClass = Literal["unmetabolized", "metabolized"]

SELECT_READS_EVIDENCE_CLASS: EvidenceClass = "fitness"

ACTIVITY_OBSERVATION_UNITS = frozenset(
    {
        "packet_sent",
        "packets_sent",
        "scout_ranked_drafts",
        "packet_not_sent",
        "packet_killed",
        "market_signal",
        "demand_hint",
        "observed_behavior",
        "world_signal",
    }
)
ACTIVITY_EVIDENCE_TYPES = frozenset({"cost", "manual_exception"})

FITNESS_EVIDENCE_TYPES = frozenset(
    {
        "reply",
        "meeting",
        "proposal",
        "commitment",
        "cash",
        "usage",
        "outcome",
        "refund",
        "complaint",
        "churn",
        "delivery_overrun",
    }
)
FITNESS_OBSERVATION_UNITS = frozenset(
    {
        "payable_covering_conversation",
        "covering_conversation",
        "customer_outcome",
        "collected_cash",
        "signed_commitment",
        "verified_outcome",
        "retained_usage",
    }
)


def _observation_unit(evidence: Mapping[str, Any]) -> str:
    observation = evidence.get("observation")
    if not isinstance(observation, Mapping):
        return ""
    unit = observation.get("unit")
    return str(unit).strip().lower() if unit is not None else ""


def _evidence_type(evidence: Mapping[str, Any]) -> str:
    value = evidence.get("type")
    return str(value).strip().lower() if value is not None else ""


def classify_evidence(evidence: Mapping[str, Any]) -> EvidenceClass | None:
    """Return ``activity``, ``fitness``, or ``None`` when the observation is neither.

    Named activity and Sense units win even if the envelope type is ``other``.
    Fitness types and named fitness units count toward ``fitness-v1`` classes.
    Immune types such as ``security_incident`` and ``policy_violation`` stay
    unclassified here; they already have their own stop path.
    """

    unit = _observation_unit(evidence)
    evidence_type = _evidence_type(evidence)
    if unit in ACTIVITY_OBSERVATION_UNITS or evidence_type in ACTIVITY_EVIDENCE_TYPES:
        return "activity"
    if evidence_type in FITNESS_EVIDENCE_TYPES or unit in FITNESS_OBSERVATION_UNITS:
        return "fitness"
    return None


def metabolic_class(evidence: Mapping[str, Any]) -> MetabolicClass | None:
    """Transform view of the same grammar: fitness is metabolized, activity is not."""

    classified = classify_evidence(evidence)
    if classified == "activity":
        return "unmetabolized"
    if classified == "fitness":
        return "metabolized"
    return None


__all__ = [
    "ACTIVITY_EVIDENCE_TYPES",
    "ACTIVITY_OBSERVATION_UNITS",
    "FITNESS_EVIDENCE_TYPES",
    "FITNESS_OBSERVATION_UNITS",
    "SELECT_READS_EVIDENCE_CLASS",
    "classify_evidence",
    "metabolic_class",
]

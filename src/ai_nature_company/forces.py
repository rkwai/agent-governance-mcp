"""Eight natural forces as substrate physics.

Bound, Sense, Act, Transform, Maintain, Defend, Select, and Continue are
persistent forces, not roles, cast seats, chat personas, or org-chart jobs.
The MCP server is the physics engine. Hosts and agents are temporary particles
inside the forces. The same eight forces operate similarly at organism (offer
cell), colony (company portfolio), and civilization (founder genome) scales.

Signed cast names and sit-the-seat principals are optional effectors mapped
under these forces. They are not the architecture.

Routine force handoffs are MCP writes plus reads. Agent-to-agent chat is
out-of-band. Channel membership is not a work queue. Force-peer silence is
not silence toward founder, humans, or other external entities.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal

FORCE_NAMES = (
    "bound",
    "sense",
    "act",
    "transform",
    "maintain",
    "defend",
    "select",
    "continue",
)

SCALES = ("organism", "colony", "civilization")

LEDGER_HANDOFF: MappingProxyType[str, object] = MappingProxyType(
    {
        "default": "write_ledger_then_next_force_reads",
        "after_mutate_that_advances": ["cell", "run", "evidence", "lease", "tries"],
        "next_step": ["stop", "read_back"],
        "read_back": ["company.describe", "cell_records"],
        "not": ["peer_dm", "agent_chat", "send_to_agent"],
        "agent_to_agent_chat": "out_of_band",
        "out_of_band_when": [
            "founder_facing_exceptions",
            "true_blockers",
            "founder_requested_digests",
        ],
        "channel_membership_is_not": "work_queue",
        "grok_chat_is_not": "the_ledger",
        "does_not_mean": "silent_to_everyone",
        "external_entity_chat": "normal_and_distinct_from_force_peer_silence",
        "external_entities": ["founder", "humans", "non_force_agents"],
        "external_entities_are_not": "anc_ledger_peers",
        "host_MUST_when_external_waiting": [
            "acknowledge",
            "do_the_work",
            "wake_or_reply",
        ],
    }
)

LEDGER_HANDOFF_AFTER_MUTATE = (
    " The ledger write is the handoff; the next force reads MCP. Then stop or "
    "read back via company.describe / cell_records. Do not DM a peer or use "
    "agent chat for the routine force-peer handoff. That silence is not "
    "silence toward founder, humans, or other external entities; acknowledge "
    "and reply when they are waiting."
)

SENSE_WORK_KINDS = frozenset({"sense_market"})
ACT_WORK_KINDS = frozenset({"scout_market", "sales_step", "delivery_step"})

WorkForce = Literal["sense", "act"]


def work_kind_force(kind: object) -> WorkForce:
    """Classify leased work as Sense (no try debit) or Act (side-effect cost).

    Only named Sense kinds are observation. Every other work kind that completes
    or fails with a positive cost is Act, including generic collect/delivery work.
    """

    if str(kind or "").strip() in SENSE_WORK_KINDS:
        return "sense"
    return "act"


def describe_natural_forces() -> dict[str, object]:
    """Return the host-visible physics contract for ``protocol.describe``."""

    return {
        "kind": "physics",
        "not": [
            "roles",
            "cast_seats",
            "chat_personas",
            "org_chart_jobs",
        ],
        "mcp_is": "physics_engine",
        "hosts_and_agents": "temporary_particles_inside_forces",
        "hermetic": "as_above_so_below",
        "scales": list(SCALES),
        "founding_topology": (
            "one_owner_per_outcome_temporary_specialists_independent_verification_shared_evidence"
        ),
        "handoff": dict(LEDGER_HANDOFF),
        "names": list(FORCE_NAMES),
        "forces": {name: dict(spec) for name, spec in NATURAL_FORCES.items()},
    }


def describe_effectors() -> dict[str, object]:
    """Optional particles inside forces — never the eight-force architecture."""

    return {
        "kind": "optional_particles_inside_forces",
        "not": "the_eight_forces",
        "signed_cast": "labeled definitions mapped under forces; not standing personas",
        "sit_the_seat": {
            "live_principals": ["owner", "specialist", "evaluator"],
            "owner": {
                "role": "cell_owner",
                "effectuates": "bound",
                "determines": ["icp", "targets"],
                "does_not_impersonate": "select",
                "learns": "icp_target_offer_adaptation",
            },
            "sense": {
                "role": "specialist",
                "path": "second_specialist_effector_path",
                "effectuates": "sense",
                "work_kind": "sense_market",
                "debits_try_spend": False,
                "does": ["lists_within_bound_icp_target_frame", "enrich", "packets"],
                "learns": [
                    "list_quality",
                    "enrich_quality",
                    "door_truth",
                    "buyer_identity_signal",
                ],
                "not_a_new_force": True,
            },
            "hunter": {
                "role": "specialist",
                "effectuates": "act",
                "work_kinds": ["scout_market", "sales_step", "delivery_step"],
                "requires": "sense_ready_packets",
                "external_send": "founder_sign_off",
                "debits_try_spend": True,
                "learns": "try_effectiveness_after_sense_ready",
                "does_not_learn": "list_volume",
            },
            "evaluator": {
                "role": "evaluator",
                "effectuates": "defend",
                "producer_cannot_clear_own_claims": True,
            },
            "handoff": {
                "routine": "write_ledger_then_next_force_reads",
                "send_to_agent_for_routine_handoffs": False,
                "does_not_mean": "silent_to_everyone",
                "external_entity_chat": "normal",
                "surface_to_founder_at": [
                    "sign",
                    "pause",
                    "icp_change",
                    "budget",
                    "activation_mismatch",
                ],
            },
        },
    }


NATURAL_FORCES: MappingProxyType[str, MappingProxyType[str, object]] = MappingProxyType(
    {
        "bound": MappingProxyType(
            {
                "law": "identity_membrane_permitted_flows_fail_closed_grants",
                "inside_vs_outside": True,
                "tools": [
                    "company.bootstrap",
                    "constitution.get",
                    "constitution.amend",
                    "agent.register",
                    "cell.amend",
                ],
                "organism": (
                    "cell identity, ICP and targets, offer membrane, and cell-scoped grants"
                ),
                "colony": "company constitution, membership, and fail-closed grants",
                "civilization": "founder genome and irreversible authority",
            }
        ),
        "sense": MappingProxyType(
            {
                "law": "observe_world_market_client_signal_into_shared_medium",
                "debits_try_spend": False,
                "work_kinds": sorted(SENSE_WORK_KINDS),
                "organism": "lists, enrich, and packets inside Bound ICP/target frame",
                "colony": "cross-cell shared evidence reads",
                "civilization": "accumulated verified market history",
            }
        ),
        "act": MappingProxyType(
            {
                "law": "side_effects_that_change_the_world_cost_tries_or_budget",
                "debits_try_spend": True,
                "work_kinds": sorted(ACT_WORK_KINDS),
                "tools": ["experiment.record_send"],
                "recorded_external_send": "exactly_one_try",
                "external_message_cap": "max_external_messages_per_experiment",
                "organism": "leased reflex step with recorded cost",
                "colony": "portfolio experiments that spend exploration or exploitation",
                "civilization": "founder-gated irreversible bets",
            }
        ),
        "transform": MappingProxyType(
            {
                "law": "sense_and_act_must_metabolize_into_structured_evidence_or_economics",
                "metabolized": "fitness",
                "unmetabolized": "activity",
                "select_reads": "fitness_evidence_count",
                "select_does_not_read": "activity_evidence_count",
                "organism": "cell evidence and economics envelopes",
                "colony": "shared pheromone ledger",
                "civilization": "institutional memory candidates",
            }
        ),
        "maintain": MappingProxyType(
            {
                "law": "homeostasis_leases_overdue_repair_harness_health",
                "tick": ["expired_work_leases", "overdue_open_work"],
                "tools": ["work.cancel"],
                "organism": "work leases and overdue open work",
                "colony": "process-owned ai-nature-tick hygiene",
                "civilization": "regulatory and security posture",
            }
        ),
        "defend": MappingProxyType(
            {
                "law": "independent_verify_producer_cannot_clear_own_claims",
                "producer_equals_verifier": False,
                "tools": ["evaluation.submit", "gate.evaluate"],
                "organism": "evaluator != executor of the same experiment",
                "colony": "independent closeout across cells",
                "civilization": "human verification of irreversible action",
            }
        ),
        "select": MappingProxyType(
            {
                "law": "allocation_from_fitness_not_activity",
                "formula": "fitness-v1",
                "eligibility_before_rank": True,
                "tools": ["allocation.propose", "allocation.commit"],
                "organism": "cell micro-P&L after eligibility gates",
                "colony": "allocator ranks cells; owner-led contain is not portfolio select",
                "civilization": "founder chooses arena, capital, and kill",
            }
        ),
        "continue": MappingProxyType(
            {
                "law": "apoptosis_evidence_retention_gated_learning_decay",
                "tick": ["due_experiment_closeout", "closeout_evaluation_work"],
                "tools": [
                    "experiment.amend",
                    "experiment.close",
                    "learning.propose",
                    "learning.promote",
                ],
                "organism": "stop rules, failed-experiment retention, gated lessons",
                "colony": "pause, kill, seed bank; evidence stays inspectable",
                "civilization": "constitution versions, rollback, decay of obsolete strategy",
            }
        ),
    }
)


__all__ = [
    "ACT_WORK_KINDS",
    "FORCE_NAMES",
    "LEDGER_HANDOFF",
    "LEDGER_HANDOFF_AFTER_MUTATE",
    "NATURAL_FORCES",
    "SCALES",
    "SENSE_WORK_KINDS",
    "describe_effectors",
    "describe_natural_forces",
    "work_kind_force",
]

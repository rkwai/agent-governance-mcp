"""Optional effectors mapped under the eight natural forces.

These labeled definitions are particles inside Bound, Sense, Act, Transform,
Maintain, Defend, Select, and Continue. They are not the eight forces, not
standing personas, and not an org chart. Immune, keeper, and strategy remain
un-minted. The substrate does not invoke a model.
"""

from __future__ import annotations

from types import MappingProxyType

from .forces import ACT_WORK_KINDS, SENSE_WORK_KINDS

SIGNED_CAST: MappingProxyType[str, MappingProxyType[str, object]] = MappingProxyType(
    {
        "market-scout": MappingProxyType(
            {
                "signed_as": "market-scout",
                "compatible_role": "specialist",
                "effectuates": "act",
                "work_kind": "scout_market",
                "requires": "sense_ready_packets",
                "routine": "reflex",
                "not_a_force": True,
                "reward": "signals_that_become_validated_demand",
                "anti_reward": "lead_volume",
            }
        ),
        "cell-owner": MappingProxyType(
            {
                "signed_as": "offer-designer",
                "compatible_role": "cell_owner",
                "effectuates": "bound",
                "does_not_impersonate": "select",
                "routine": "adaptation",
                "not_a_force": True,
                "reward": "improvement_over_registered_baseline",
                "anti_reward": "idea_volume",
            }
        ),
        "sales": MappingProxyType(
            {
                "signed_as": "sales",
                "compatible_role": "specialist",
                "effectuates": "act",
                "routine": "reflex",
                "not_a_force": True,
                "reward": "collected_contribution_profit",
                "anti_reward": "bookings_alone",
            }
        ),
        "delivery": MappingProxyType(
            {
                "signed_as": "delivery",
                "compatible_role": "specialist",
                "effectuates": "act",
                "routine": "reflex",
                "not_a_force": True,
                "reward": "verified_outcome_per_delivery_cost",
                "anti_reward": "tasks_completed",
            }
        ),
        "evaluator": MappingProxyType(
            {
                "signed_as": "experiment-evaluator",
                "compatible_role": "evaluator",
                "effectuates": "defend",
                "routine": "selection",
                "not_a_force": True,
                "reward": "calibration_and_cheap_falsification",
                "anti_reward": "declaring_experiments_successful",
            }
        ),
        "memory": MappingProxyType(
            {
                "signed_as": "memory",
                "compatible_role": "specialist",
                "effectuates": "continue",
                "routine": "evolution",
                "not_a_force": True,
                "reward": "future_performance_uplift",
                "anti_reward": "memory_volume",
            }
        ),
        "allocator": MappingProxyType(
            {
                "signed_as": "allocator",
                "compatible_role": "allocator",
                "effectuates": "select",
                "routine": "selection",
                "not_a_force": True,
                "reward": "portfolio_profit_survival_learning_efficiency",
                "anti_reward": "active_agent_count",
            }
        ),
    }
)

REFLEX_WORK_KINDS = SENSE_WORK_KINDS | ACT_WORK_KINDS
REFLEX_FORBIDDEN_CAPABILITIES = frozenset(
    {
        "allocation.commit",
        "allocation.propose",
        "evaluation.submit",
        "learning.promote",
    }
)

CELL_ROUTINES: MappingProxyType[str, MappingProxyType[str, object]] = MappingProxyType(
    {
        "reflex": MappingProxyType(
            {
                "cadence": "minutes_to_hours",
                "controller": "leased_work",
                "forces": ["sense", "act"],
                "work_kinds": sorted(REFLEX_WORK_KINDS),
                "sense_work_kinds": sorted(SENSE_WORK_KINDS),
                "act_work_kinds": sorted(ACT_WORK_KINDS),
                "effectors": ["market-scout", "sales", "delivery"],
                "handoff": "write_ledger_then_next_force_reads",
                "not": "ai-nature-tick",
            }
        ),
        "adaptation": MappingProxyType(
            {
                "cadence": "daily",
                "controller": "offer.mutate",
                "forces": ["bound", "transform"],
                "effector": "offer-designer",
                "role": "cell_owner",
                "price_floor_enforced": True,
                "immutable_children": True,
            }
        ),
        "selection": MappingProxyType(
            {
                "cadence": "weekly",
                "controller": "allocator",
                "forces": ["select", "defend"],
                "formula": "fitness-v1",
                "eligibility_before_rank": True,
                "separate_exploration_exploitation_ledgers": True,
                "kernel_fallback": "owner_led_allocate_contain",
                "tick": "stop_rule_closeout_not_reflex",
            }
        ),
        "evolution": MappingProxyType(
            {
                "cadence": "monthly_or_strong_evidence",
                "controller": "founder",
                "forces": ["continue", "bound"],
                "requires_founder": [
                    "constitution",
                    "irreversible_kill",
                    "major_capital",
                    "high_risk_entry",
                ],
            }
        ),
    }
)


def signed_definition_names() -> frozenset[str]:
    return frozenset(SIGNED_CAST)


__all__ = [
    "CELL_ROUTINES",
    "REFLEX_FORBIDDEN_CAPABILITIES",
    "REFLEX_WORK_KINDS",
    "SIGNED_CAST",
    "signed_definition_names",
]

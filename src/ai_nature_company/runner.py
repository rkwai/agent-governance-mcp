"""External Phase 1 agent-host demonstration over the official MCP boundary.

The runner intentionally contains no inference, provider, or scheduling integration.
It supplies deterministic demo decisions as if separate agents had made them, then
uses an in-process :class:`mcp.client.Client` for every substrate interaction.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.client import Client

from .auth import (
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
    is_verified_principal,
)
from .server import StaticAuthResolver, SubstrateService, build_server

Clock = Callable[[], datetime]

COMPANY_ID = "71000000-0000-4000-8000-000000000001"
CONSTITUTION_ID = "71000000-0000-4000-8000-000000000002"
COMPANY_BUDGET_ID = "71000000-0000-4000-8000-000000000003"
CELL_ID = "71000000-0000-4000-8000-000000000004"
CELL_BUDGET_ID = "71000000-0000-4000-8000-000000000005"
OFFER_ID = "71000000-0000-4000-8000-000000000006"
OFFER_VERSION_ID = "71000000-0000-4000-8000-000000000007"
MUTATED_OFFER_VERSION_ID = "71000000-0000-4000-8000-000000000008"
EXPERIMENT_ID = "71000000-0000-4000-8000-000000000009"
WORK_ITEM_ID = "71000000-0000-4000-8000-000000000010"
EVIDENCE_ID = "71000000-0000-4000-8000-000000000011"
EVALUATION_ID = "71000000-0000-4000-8000-000000000012"
ALLOCATION_ID = "71000000-0000-4000-8000-000000000013"
CORRELATION_ID = "71000000-0000-4000-8000-000000000014"

PRINCIPAL_IDS: Mapping[str, str] = {
    "founder": "72000000-0000-4000-8000-000000000001",
    "owner": "72000000-0000-4000-8000-000000000002",
    "specialist": "72000000-0000-4000-8000-000000000003",
    "evaluator": "72000000-0000-4000-8000-000000000004",
    "allocator": "72000000-0000-4000-8000-000000000005",
}
RUN_IDS: Mapping[str, str] = {
    "founder": "74000000-0000-4000-8000-000000000001",
    "owner": "74000000-0000-4000-8000-000000000002",
    "specialist": "74000000-0000-4000-8000-000000000003",
    "evaluator": "74000000-0000-4000-8000-000000000004",
    "allocator": "74000000-0000-4000-8000-000000000005",
}

_AGENTS_ROOT = Path(__file__).resolve().parents[2] / "agents"

_ROLES: Mapping[str, Role] = {
    "founder": Role.FOUNDER,
    "owner": Role.CELL_OWNER,
    "specialist": Role.SPECIALIST,
    "evaluator": Role.EVALUATOR,
    "allocator": Role.ALLOCATOR,
}


class VerticalSliceError(RuntimeError):
    """An MCP operation failed or returned an invalid runner-facing contract."""


def build_demo_principals() -> dict[str, VerifiedPrincipal]:
    """Issue fixed demo identities through the trusted local authentication boundary."""

    scope = ResourceScope.company(COMPANY_ID)
    credentials: dict[str, PrincipalRecord] = {}
    for name, role in _ROLES.items():
        credentials[f"vertical-slice-{name}"] = PrincipalRecord(
            principal_id=PRINCIPAL_IDS[name],
            grants=(ScopedRoleGrant(role, scope),),
            agent_run_id=RUN_IDS[name],
        )
    authenticator = LocalTestAuthenticator(credentials)
    return {name: authenticator.authenticate(f"vertical-slice-{name}") for name in _ROLES}


def _validate_principals(
    principals: Mapping[str, VerifiedPrincipal],
) -> dict[str, VerifiedPrincipal]:
    missing = set(_ROLES) - set(principals)
    if missing:
        raise TypeError(f"principals missing runner roles: {', '.join(sorted(missing))}")
    selected = {name: principals[name] for name in _ROLES}
    for name, principal in selected.items():
        if not is_verified_principal(principal):
            raise TypeError(f"{name} must be a VerifiedPrincipal")
        if principal.company_ids != frozenset({COMPANY_ID}):
            raise TypeError(f"{name} must have exactly the demo company scope")
        if _ROLES[name] not in principal.roles:
            raise TypeError(f"{name} lacks its required role")
        if principal.agent_run_id is None:
            raise TypeError(f"{name} must have a trusted agent run id")
    return selected


def _now(clock: Clock | None) -> datetime:
    value = clock() if clock is not None else datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _envelope(
    tool_name: str,
    key: str,
    expected_revision: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "expected_revision": expected_revision,
        "correlation_id": CORRELATION_ID,
        "operation_intent": tool_name,
        "payload": dict(payload),
    }


async def _call(
    service: SubstrateService,
    principal: VerifiedPrincipal,
    tool_name: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one tool through an official in-process MCP client and check its result."""

    server = build_server(service, StaticAuthResolver(principal))
    arguments = {"payload": dict(payload)} if payload is not None else {}
    async with Client(server) as client:
        result = await client.call_tool(tool_name, arguments)
    if result.is_error:
        detail = " ".join(str(getattr(item, "text", "")) for item in result.content).strip()
        raise VerticalSliceError(f"{tool_name} failed: {detail or 'unknown MCP tool error'}")
    if not isinstance(result.structured_content, Mapping):
        raise VerticalSliceError(f"{tool_name} returned no structured object")
    return dict(result.structured_content)


def _state_record(
    result: Mapping[str, Any],
    key: str,
    tool_name: str,
) -> dict[str, Any]:
    state = result.get("state")
    record = state.get(key) if isinstance(state, Mapping) else None
    if not isinstance(record, Mapping):
        raise VerticalSliceError(f"{tool_name} returned no {key!r} state")
    return dict(record)


def _state_revision(result: Mapping[str, Any], key: str, tool_name: str) -> int:
    revision = _state_record(result, key, tool_name).get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise VerticalSliceError(f"{tool_name} returned no integer {key} revision")
    return revision


def _offer_revision(result: Mapping[str, Any], tool_name: str) -> int:
    offer = _state_record(result, "offer", tool_name)
    revision = offer.get("revision")
    if revision is None:
        version = offer.get("version")
        revision = (
            version - 1 if isinstance(version, int) and not isinstance(version, bool) else None
        )
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise VerticalSliceError(f"{tool_name} returned no integer offer revision")
    return revision


def _lease_token(result: Mapping[str, Any]) -> str:
    state = result.get("state")
    lease = state.get("lease") if isinstance(state, Mapping) else None
    if isinstance(lease, Mapping):
        token = lease.get("token") or lease.get("lease_token")
        if isinstance(token, str) and token:
            return token
    raise VerticalSliceError("work.claim returned no lease token")


def _gate_decision_id(result: Mapping[str, Any]) -> str:
    state = result.get("state")
    if not isinstance(state, Mapping):
        raise VerticalSliceError("gate.evaluate returned no state")
    decision = state.get("decision", state.get("gate_result", state))
    if not isinstance(decision, Mapping):
        raise VerticalSliceError("gate.evaluate returned no decision")
    decision_id = decision.get("decision_id", decision.get("gate_result_id"))
    if not isinstance(decision_id, str) or not decision_id:
        raise VerticalSliceError("gate.evaluate returned no decision id")
    return decision_id


def _receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a content-minimized receipt and deliberately omit lease credentials."""

    return {
        key: result[key]
        for key in (
            "operation_id",
            "prior_revision",
            "new_revision",
            "event_ids",
            "audit_id",
            "replayed",
        )
        if key in result
    }


def _load_agent_definition(kind: str) -> dict[str, Any]:
    path = _AGENTS_ROOT / kind / "agent-definition.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerticalSliceError(f"cannot load agent definition {kind!r}") from error
    if not isinstance(value, dict):
        raise VerticalSliceError(f"agent definition {kind!r} is not a JSON object")
    return value


def _bootstrap_payload() -> dict[str, Any]:
    definitions = {
        "owner": "cell-owner",
        "specialist": "specialist",
        "evaluator": "evaluator",
        "allocator": "allocator",
    }
    agents = [
        {
            "principal_id": PRINCIPAL_IDS[name],
            "cell_id": CELL_ID if name in {"owner", "specialist", "evaluator"} else None,
            "definition": _load_agent_definition(kind),
        }
        for name, kind in definitions.items()
    ]
    return {
        "company_id": COMPANY_ID,
        "name": "AI Nature Company",
        "constitution": {
            "schema_version": 1,
            "constitution_id": CONSTITUTION_ID,
            "version": 1,
            "mission": "Create verified customer value and contribution profit.",
            "authority_policy": {
                "founder_principal_ids": [PRINCIPAL_IDS["founder"]],
                "irreversible_actions": ["company.dissolve"],
                "human_approval_actions": ["constitution.activate"],
            },
            "risk_policy": {
                "prohibited_actions": ["claim.fabricate"],
                "pricing_floor_minor": 10000,
                "max_external_messages_per_experiment": 10,
                "regulated_market_entry_requires_founder": True,
            },
            "budget_policy": {
                "currency": "USD",
                "company_limit_minor": 1000000,
                "cell_limit_minor": 100000,
                "experiment_limit_minor": 10000,
                "single_action_limit_minor": 10000,
                "exploration_share_bps": 2000,
            },
            "fitness_policy": {
                "rule_version": "fitness-v1",
                "required_gates": [
                    "legality_and_policy",
                    "truthfulness_and_claim_support",
                    "customer_outcome_floor",
                    "security_and_privacy",
                    "budget_and_cash",
                    "delivery_quality_floor",
                    "data_sufficiency_and_independence",
                ],
                "minimum_evidence_count": 1,
                "minimum_customer_outcome_bps": 8000,
                "minimum_delivery_quality_bps": 8000,
                "allocation_cadence_days": 7,
            },
        },
        "budget": {
            "budget_id": COMPANY_BUDGET_ID,
            "limit": "10000.00",
            "currency": "USD",
            "duration_days": 365,
        },
        "agents": agents,
    }


def _offer_payload() -> dict[str, Any]:
    return {
        "cell_id": CELL_ID,
        "offer_id": OFFER_ID,
        "offer_version_id": OFFER_VERSION_ID,
        "target_segment": "Owner-led service firms",
        "urgent_problem": "Bespoke delivery constrains margin and sales speed.",
        "promised_outcome": "Package one repeatable workflow in four weeks.",
        "proof": ["Prior delivery-cycle reduction was independently verified."],
        "price": {"currency": "USD", "amount_minor": 500000},
        "commercial_model": "fixed_fee",
        "acquisition_channel": "Founder-led referral",
        "delivery_recipe": ["Map workflow", "Run bounded pilot", "Verify outcome"],
        "time_to_value_days": 28,
        "risk_reversal": "Stop after the sample if verified evidence does not hold.",
    }


def _offer_mutation_payload() -> dict[str, Any]:
    return {
        "parent_offer_version_id": OFFER_VERSION_ID,
        "offer_version_id": MUTATED_OFFER_VERSION_ID,
        "mutation_summary": "Narrowed the segment and raised the proof-backed price.",
        "changes": {
            "target_segment": "Owner-led data and engineering service firms",
            "price": {"currency": "USD", "amount_minor": 600000},
        },
    }


def _experiment_payload(at: datetime) -> dict[str, Any]:
    return {
        "experiment_id": EXPERIMENT_ID,
        "cell_id": CELL_ID,
        "offer_version_id": MUTATED_OFFER_VERSION_ID,
        "evaluator_principal_id": PRINCIPAL_IDS["evaluator"],
        "hypothesis": "A verified sample will produce one paid-pilot request.",
        "baseline": "No paid-pilot requests from zero samples.",
        "treatment": "Deliver one bounded sample to ten eligible firms.",
        "target_population": "Ten owner-led service firms matching the frozen ICP.",
        "metrics": [
            {
                "metric_id": "paid_pilot_requests",
                "name": "Paid pilot requests",
                "unit": "count",
                "direction": "increase",
            }
        ],
        "success_thresholds": [{"metric_id": "paid_pilot_requests", "operator": "gte", "value": 1}],
        "failure_thresholds": [{"metric_id": "paid_pilot_requests", "operator": "eq", "value": 0}],
        "stop_rules": [
            {
                "rule_id": "budget_exhausted",
                "condition": "Actual cost reaches the fixed experiment budget.",
                "effect": "pause_external_actions",
            }
        ],
        "window": {
            "starts_at": _timestamp(at),
            "ends_at": _timestamp(at + timedelta(days=7)),
        },
        "budget": {"currency": "USD", "amount_minor": 10000},
        "permitted_actions": ["evidence.append"],
        "executor_principal_ids": [PRINCIPAL_IDS["specialist"]],
    }


def _work_payload(at: datetime) -> dict[str, Any]:
    return {
        "work_item_id": WORK_ITEM_ID,
        "cell_id": CELL_ID,
        "experiment_id": EXPERIMENT_ID,
        "kind": "collect_verified_sample",
        "outcome_contract": {
            "outcome": "Record one attributable customer outcome.",
            "acceptance_criteria": ["Evidence cites the simulated CRM receipt."],
            "result_schema_ref": "schemas/evidence-v1.json",
        },
        "eligible_capabilities": ["evidence.append"],
        "budget": {"currency": "USD", "maximum_cost_minor": 1000},
        "approval_requirements": ["none"],
        "input_references": [
            {"kind": "offer_version", "id": MUTATED_OFFER_VERSION_ID, "revision": 2}
        ],
        "max_attempts": 3,
        "available_at": _timestamp(at),
        "deadline_at": _timestamp(at + timedelta(days=7)),
    }


def _evidence_payload(at: datetime) -> dict[str, Any]:
    return {
        "evidence_id": EVIDENCE_ID,
        "cell_id": CELL_ID,
        "offer_version_id": MUTATED_OFFER_VERSION_ID,
        "experiment_id": EXPERIMENT_ID,
        "type": "outcome",
        "source_system": "simulated_crm",
        "source_reference": "simulated_crm:outcome:1",
        "observed_at": _timestamp(at),
        "strength": 1,
        "confidence_bps": 9000,
        "provenance_quality": "verified",
        "causal_distance": "direct",
        "half_life_seconds": 604800,
        "observation": {
            "value": 9000,
            "unit": "basis_points",
            "summary": "A verified customer outcome met the frozen threshold.",
        },
        "lineage": {"relation": "original", "related_evidence_id": None},
    }


def _evaluation_payload(experiment_revision: int, window_ends_at: str) -> dict[str, Any]:
    return {
        "evaluation_id": EVALUATION_ID,
        "experiment_id": EXPERIMENT_ID,
        "experiment_revision": experiment_revision,
        "executor_principal_ids": [PRINCIPAL_IDS["specialist"]],
        "window_ends_at": window_ends_at,
        "criterion_results": [
            {
                "criterion_id": "paid_pilot_requests",
                "outcome": "passed",
                "observed_value": 1,
                "evidence_ids": [EVIDENCE_ID],
            }
        ],
        "result": "passed",
        "summary": "The frozen paid-pilot-request threshold was met.",
    }


async def run_vertical_slice(
    service: SubstrateService | None = None,
    principals: Mapping[str, VerifiedPrincipal] | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Run one profitable-offer learning loop entirely through MCP tools."""

    if clock is None:

        def runtime_clock() -> datetime:
            return datetime.now(UTC)

    else:
        runtime_clock = clock
    if service is None:
        from .service import create_service

        service = create_service(clock=runtime_clock)
    actors = _validate_principals(build_demo_principals() if principals is None else principals)
    at = _now(runtime_clock)
    receipts: dict[str, Any] = {}

    bootstrap = await _call(
        service,
        actors["founder"],
        "company.bootstrap",
        _envelope("company.bootstrap", "demo-bootstrap-v1", 0, _bootstrap_payload()),
    )
    receipts["bootstrap"] = _receipt(bootstrap)

    cell = await _call(
        service,
        actors["owner"],
        "cell.create",
        _envelope(
            "cell.create",
            "demo-cell-v1",
            0,
            {
                "cell_id": CELL_ID,
                "budget_id": CELL_BUDGET_ID,
                "name": "Service Workflow Productization",
                "objective": "Find a repeatable, profitable workflow-productization offer.",
                "budget_limit": "1000.00",
                "currency": "USD",
                "budget_duration_days": 90,
            },
        ),
    )
    receipts["cell_create"] = _receipt(cell)

    offer = await _call(
        service,
        actors["owner"],
        "offer.register",
        _envelope(
            "offer.register",
            "demo-offer-v1",
            _state_revision(cell, "cell", "cell.create"),
            _offer_payload(),
        ),
    )
    receipts["offer_register"] = _receipt(offer)
    cell_revision = _state_revision(offer, "cell", "offer.register")

    mutated_offer = await _call(
        service,
        actors["owner"],
        "offer.mutate",
        _envelope(
            "offer.mutate",
            "demo-offer-v2",
            _offer_revision(offer, "offer.register"),
            _offer_mutation_payload(),
        ),
    )
    receipts["offer_mutate"] = _receipt(mutated_offer)
    cell_revision = _state_revision(mutated_offer, "cell", "offer.mutate")

    ready = await _call(
        service,
        actors["owner"],
        "cell.transition",
        _envelope(
            "cell.transition",
            "demo-cell-ready-v1",
            cell_revision,
            {"cell_id": CELL_ID, "target_status": "ready"},
        ),
    )
    receipts["cell_ready"] = _receipt(ready)

    active = await _call(
        service,
        actors["owner"],
        "cell.transition",
        _envelope(
            "cell.transition",
            "demo-cell-active-v1",
            _state_revision(ready, "cell", "cell.transition"),
            {"cell_id": CELL_ID, "target_status": "active"},
        ),
    )
    receipts["cell_active"] = _receipt(active)

    experiment = await _call(
        service,
        actors["owner"],
        "experiment.register",
        _envelope(
            "experiment.register",
            "demo-experiment-v1",
            0,
            _experiment_payload(at),
        ),
    )
    receipts["experiment_register"] = _receipt(experiment)

    started = await _call(
        service,
        actors["owner"],
        "experiment.start",
        _envelope(
            "experiment.start",
            "demo-experiment-start-v1",
            _state_revision(experiment, "experiment", "experiment.register"),
            {"experiment_id": EXPERIMENT_ID},
        ),
    )
    receipts["experiment_start"] = _receipt(started)

    work = await _call(
        service,
        actors["owner"],
        "work.create",
        _envelope("work.create", "demo-work-v1", 0, _work_payload(at)),
    )
    receipts["work_create"] = _receipt(work)

    claim = await _call(
        service,
        actors["specialist"],
        "work.claim",
        _envelope(
            "work.claim",
            "demo-work-claim-v1",
            _state_revision(work, "work_item", "work.create"),
            {"work_item_id": WORK_ITEM_ID, "lease_seconds": 300},
        ),
    )
    receipts["work_claim"] = _receipt(claim)
    lease_token = _lease_token(claim)

    evidence = await _call(
        service,
        actors["specialist"],
        "evidence.append",
        _envelope("evidence.append", "demo-evidence-v1", 0, _evidence_payload(at)),
    )
    receipts["evidence_append"] = _receipt(evidence)

    completed = await _call(
        service,
        actors["specialist"],
        "work.complete",
        _envelope(
            "work.complete",
            "demo-work-complete-v1",
            _state_revision(claim, "work_item", "work.claim"),
            {
                "work_item_id": WORK_ITEM_ID,
                "lease_token": lease_token,
                "result_evidence_id": EVIDENCE_ID,
                "actual_cost": {"currency": "USD", "amount_minor": 500},
            },
        ),
    )
    receipts["work_complete"] = _receipt(completed)

    closed = await _call(
        service,
        actors["owner"],
        "experiment.close",
        _envelope(
            "experiment.close",
            "demo-experiment-close-v1",
            _state_revision(started, "experiment", "experiment.start"),
            {
                "experiment_id": EXPERIMENT_ID,
                "actual_cost": {"currency": "USD", "amount_minor": 500},
            },
        ),
    )
    receipts["experiment_close"] = _receipt(closed)
    experiment_revision = _state_revision(closed, "experiment", "experiment.close")

    evaluation = await _call(
        service,
        actors["evaluator"],
        "evaluation.submit",
        _envelope(
            "evaluation.submit",
            "demo-evaluation-v1",
            experiment_revision,
            _evaluation_payload(
                experiment_revision,
                str(_experiment_payload(at)["window"]["ends_at"]),
            ),
        ),
    )
    receipts["evaluation_submit"] = _receipt(evaluation)

    gate = await _call(
        service,
        actors["allocator"],
        "gate.evaluate",
        _envelope(
            "gate.evaluate",
            "demo-gate-v1",
            0,
            {
                "action": "fund",
                "stage": "exploitation",
                "target": {
                    "kind": "cell",
                    "id": CELL_ID,
                    "revision": _state_revision(active, "cell", "cell.transition"),
                },
                "evaluation_id": EVALUATION_ID,
                "amount": {"currency": "USD", "amount_minor": 9000},
            },
        ),
    )
    receipts["gate_evaluate"] = _receipt(gate)

    allocation = await _call(
        service,
        actors["allocator"],
        "allocation.propose",
        _envelope(
            "allocation.propose",
            "demo-allocation-v1",
            0,
            {
                "allocation_id": ALLOCATION_ID,
                "action": "fund",
                "target": {
                    "kind": "cell",
                    "id": CELL_ID,
                    "revision": _state_revision(active, "cell", "cell.transition"),
                },
                "stage": "exploitation",
                "gate_decision_id": _gate_decision_id(gate),
                "ranking": {
                    "formula": "exploitation-v1",
                    "input_snapshot": {
                        "expected_12m_contribution_profit_minor": 500000,
                        "evidence_confidence_bps": 9000,
                        "expected_repeatability_bps": 6000,
                        "capital_at_risk_minor": 10000,
                        "time_to_cash_days": 30,
                    },
                },
                "amount": {"currency": "USD", "amount_minor": 9000},
                "rationale_summary": "All seven gates pass under the frozen fitness rule.",
            },
        ),
    )
    receipts["allocation_propose"] = _receipt(allocation)

    committed = await _call(
        service,
        actors["allocator"],
        "allocation.commit",
        _envelope(
            "allocation.commit",
            "demo-allocation-commit-v1",
            _state_revision(allocation, "allocation", "allocation.propose"),
            {"allocation_id": ALLOCATION_ID},
        ),
    )
    receipts["allocation_commit"] = _receipt(committed)

    company = await _call(service, actors["founder"], "company.describe")
    audit = await _call(
        service,
        actors["founder"],
        "audit.list",
        {"after_position": 0, "limit": 100},
    )
    committed_allocation = _state_record(committed, "allocation", "allocation.commit")
    decision_state = committed_allocation.get("decision_state")
    if decision_state != "committed":
        raise VerticalSliceError("allocation.commit did not return a committed allocation")
    return {
        "transport": "mcp",
        "company_id": COMPANY_ID,
        "allocation_id": ALLOCATION_ID,
        "decision_state": decision_state,
        "correlation_id": CORRELATION_ID,
        "principal_ids": dict(PRINCIPAL_IDS),
        "agent_run_ids": {name: actor.agent_run_id for name, actor in actors.items()},
        "resources": {
            "cell_id": CELL_ID,
            "offer_id": OFFER_ID,
            "offer_version_id": OFFER_VERSION_ID,
            "mutated_offer_version_id": MUTATED_OFFER_VERSION_ID,
            "experiment_id": EXPERIMENT_ID,
            "work_item_id": WORK_ITEM_ID,
            "evidence_id": EVIDENCE_ID,
            "evaluation_id": EVALUATION_ID,
            "allocation_id": ALLOCATION_ID,
        },
        "receipts": receipts,
        "final_company": company.get("company"),
        "audit_event_count": len(audit.get("audit_events", [])),
    }


def main() -> None:
    """Run the deterministic in-memory demo and print its bounded receipt summary."""

    result = asyncio.run(run_vertical_slice())
    print(json.dumps(result, indent=2, sort_keys=True))


__all__ = [
    "COMPANY_ID",
    "PRINCIPAL_IDS",
    "RUN_IDS",
    "VerticalSliceError",
    "build_demo_principals",
    "main",
    "run_vertical_slice",
]


if __name__ == "__main__":  # pragma: no cover - console entry point covers this path
    main()

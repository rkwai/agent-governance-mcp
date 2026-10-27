"""Black-box fixtures shared by the numbered Phase 1 acceptance checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ai_nature_company.auth import (
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
)

ROOT = Path(__file__).resolve().parents[2]
AGENTS_ROOT = ROOT / "agents"


def stable_uuid(label: str) -> str:
    """Return readable, repeatable UUID-shaped identifiers for acceptance data."""

    return str(uuid5(NAMESPACE_URL, f"ai-nature-company:acceptance:{label}"))


COMPANY_ID = stable_uuid("company")
OTHER_COMPANY_ID = stable_uuid("other-company")
CONSTITUTION_ID = stable_uuid("constitution")
COMPANY_BUDGET_ID = stable_uuid("company-budget")
CELL_ID = stable_uuid("cell")
CELL_BUDGET_ID = stable_uuid("cell-budget")
OFFER_ID = stable_uuid("offer")
OFFER_VERSION_ID = stable_uuid("offer-v1")
MUTATED_OFFER_VERSION_ID = stable_uuid("offer-v2")
WORK_ITEM_ID = stable_uuid("work-item")
EXPERIMENT_ID = stable_uuid("experiment")
EVIDENCE_ID = stable_uuid("evidence")
EVALUATION_ID = stable_uuid("evaluation")
GATE_DECISION_ID = stable_uuid("gate-decision")
ALLOCATION_ID = stable_uuid("allocation")

PRINCIPAL_IDS = {
    "founder": stable_uuid("principal-founder"),
    "owner": stable_uuid("principal-owner"),
    "specialist": stable_uuid("principal-specialist"),
    "evaluator": stable_uuid("principal-evaluator"),
    "allocator": stable_uuid("principal-allocator"),
    "immune": stable_uuid("principal-immune"),
    "observer": stable_uuid("principal-observer"),
}

RUN_IDS = {
    name: stable_uuid(f"run-{name}")
    for name in ("founder", "owner", "specialist", "evaluator", "allocator", "immune")
}


@dataclass(slots=True)
class MutableClock:
    """Callable server clock; callers can advance it but cannot supply operation time."""

    current: datetime = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: int) -> datetime:
        self.current += timedelta(**delta)
        return self.current


def make_principal(
    role: Role,
    *,
    principal_id: str,
    company_id: str = COMPANY_ID,
    agent_run_id: str | None = None,
) -> VerifiedPrincipal:
    record = PrincipalRecord(
        principal_id,
        (ScopedRoleGrant(role, ResourceScope.company(company_id)),),
        agent_run_id=agent_run_id,
    )
    return LocalTestAuthenticator({"opaque-test-credential": record}).authenticate(
        "opaque-test-credential"
    )


def make_principals(company_id: str = COMPANY_ID) -> dict[str, VerifiedPrincipal]:
    roles = {
        "founder": Role.FOUNDER,
        "owner": Role.CELL_OWNER,
        "specialist": Role.SPECIALIST,
        "evaluator": Role.EVALUATOR,
        "allocator": Role.ALLOCATOR,
        "immune": Role.IMMUNE,
        "observer": Role.OBSERVER,
    }
    return {
        name: make_principal(
            role,
            principal_id=PRINCIPAL_IDS[name],
            company_id=company_id,
            agent_run_id=RUN_IDS.get(name),
        )
        for name, role in roles.items()
    }


def load_definition(kind: str) -> dict[str, Any]:
    return json.loads((AGENTS_ROOT / kind / "agent-definition.json").read_text(encoding="utf-8"))


def mutation(
    tool_name: str,
    payload: dict[str, Any],
    *,
    expected_revision: int,
    key: str | None = None,
    correlation: str = "acceptance",
) -> dict[str, Any]:
    """Build the normative mutation envelope from docs/PROTOCOL.md."""

    return {
        "idempotency_key": key or f"{tool_name}:{expected_revision}",
        "expected_revision": expected_revision,
        "correlation_id": stable_uuid(f"correlation:{correlation}"),
        "operation_intent": tool_name,
        "payload": payload,
    }


def assert_mutation_response(
    response: dict[str, Any],
    *,
    prior_revision: int,
    new_revision: int | None = None,
) -> dict[str, Any]:
    """Validate the stable bounded response and return its domain state."""

    assert set(response) == {
        "operation_id",
        "prior_revision",
        "new_revision",
        "event_ids",
        "state",
        "audit_id",
        "replayed",
    }
    assert response["prior_revision"] == prior_revision
    assert response["new_revision"] == (
        prior_revision + 1 if new_revision is None else new_revision
    )
    assert response["event_ids"]
    assert response["audit_id"]
    assert response["replayed"] is False
    assert isinstance(response["state"], dict)
    return response["state"]


def assert_no_hidden_reasoning(value: Any) -> None:
    """Recursively reject fields that would smuggle private model reasoning."""

    forbidden = {
        "chain_of_thought",
        "chainofthought",
        "hidden_reasoning",
        "private_scratchpad",
        "reasoning_trace",
        "reasoningtrace",
    }

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
                assert normalized not in forbidden
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)

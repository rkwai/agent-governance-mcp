"""Contract tests for the external MCP-only Phase 1 runner."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from ai_nature_company.auth import Role, VerifiedPrincipal, is_verified_principal
from ai_nature_company.errors import AuthorizationError
from ai_nature_company.runner import (
    COMPANY_ID,
    CORRELATION_ID,
    PRINCIPAL_IDS,
    RUN_IDS,
    VerticalSliceError,
    build_demo_principals,
    run_vertical_slice,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

EXPECTED_TOOLS = [
    "company.bootstrap",
    "cell.create",
    "offer.register",
    "offer.mutate",
    "cell.transition",
    "cell.transition",
    "experiment.register",
    "experiment.start",
    "work.create",
    "work.claim",
    "evidence.append",
    "work.complete",
    "experiment.close",
    "evaluation.submit",
    "gate.evaluate",
    "allocation.propose",
    "allocation.commit",
    "company.describe",
    "audit.list",
]

EXPECTED_ACTORS = [
    "founder",
    "owner",
    "owner",
    "owner",
    "owner",
    "owner",
    "owner",
    "owner",
    "owner",
    "specialist",
    "specialist",
    "specialist",
    "owner",
    "evaluator",
    "allocator",
    "allocator",
    "allocator",
    "founder",
    "founder",
]


class RecordingService:
    def __init__(self, *, reject: str | None = None) -> None:
        self.reject = reject
        self.calls: list[tuple[str, VerifiedPrincipal, dict[str, Any]]] = []
        self.revisions = {
            "company.bootstrap": 1,
            "cell.create": 1,
            "offer.register": 2,
            "offer.mutate": 1,
            "experiment.register": 1,
            "experiment.start": 2,
            "work.create": 1,
            "work.claim": 2,
            "evidence.append": 1,
            "work.complete": 3,
            "experiment.close": 3,
            "evaluation.submit": 4,
            "gate.evaluate": 1,
            "allocation.propose": 1,
            "allocation.commit": 2,
        }
        self.cell_transition_revision = 2

    async def invoke(
        self,
        tool_name: str,
        auth_context: VerifiedPrincipal,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        materialized = dict(payload)
        self.calls.append((tool_name, auth_context, materialized))
        if tool_name == self.reject:
            raise AuthorizationError("runner test rejection")
        if tool_name == "company.describe":
            return {"company": {"company_id": COMPANY_ID, "status": "active"}}
        if tool_name == "audit.list":
            return {"audit_events": [{"action": name} for name in EXPECTED_TOOLS[:-2]]}

        expected = materialized.get("expected_revision", 0)
        if tool_name == "cell.transition":
            self.cell_transition_revision += 1
            revision = self.cell_transition_revision
        else:
            revision = self.revisions[tool_name]
        state: dict[str, Any] = {}
        if tool_name == "cell.create":
            state["cell"] = {"revision": 0}
        if tool_name == "offer.register":
            state.update(offer={"version": 1}, cell={"revision": 1})
        if tool_name == "offer.mutate":
            state.update(offer={"version": 2}, cell={"revision": 2})
        if tool_name == "cell.transition":
            state["cell"] = {"revision": revision}
        if tool_name in {"experiment.register", "experiment.start", "experiment.close"}:
            state["experiment"] = {"revision": revision}
        if tool_name in {"work.create", "work.claim", "work.complete"}:
            state["work_item"] = {"revision": revision}
        if tool_name == "work.claim":
            state["lease"] = {"token": "opaque-runner-test-lease"}
        if tool_name == "gate.evaluate":
            state["decision"] = {"decision_id": "79000000-0000-4000-8000-000000000001"}
        if tool_name == "allocation.propose":
            state["allocation"] = {"revision": 1, "decision_state": "proposed"}
        if tool_name == "allocation.commit":
            state["allocation"] = {"revision": 2, "decision_state": "committed"}
        return {
            "operation_id": f"operation-{len(self.calls)}",
            "prior_revision": expected,
            "new_revision": revision,
            "event_ids": [f"event-{len(self.calls)}"],
            "state": state,
            "audit_id": f"audit-{len(self.calls)}",
            "replayed": False,
        }


def test_demo_principals_are_verified_fixed_role_and_run_contexts() -> None:
    principals = build_demo_principals()

    assert set(principals) == set(PRINCIPAL_IDS)
    for name, principal in principals.items():
        assert is_verified_principal(principal)
        assert principal.principal_id == PRINCIPAL_IDS[name]
        assert principal.agent_run_id == RUN_IDS[name]
        assert principal.company_ids == frozenset({COMPANY_ID})

    assert principals["founder"].roles == frozenset({Role.FOUNDER})
    assert principals["owner"].roles == frozenset({Role.CELL_OWNER})
    assert principals["specialist"].roles == frozenset({Role.SPECIALIST})
    assert principals["evaluator"].roles == frozenset({Role.EVALUATOR})
    assert principals["allocator"].roles == frozenset({Role.ALLOCATOR})


async def test_runner_drives_complete_role_separated_slice_over_mcp() -> None:
    service = RecordingService()

    result = await run_vertical_slice(service, clock=lambda: NOW)

    assert [name for name, _, _ in service.calls] == EXPECTED_TOOLS
    assert [actor.principal_id for _, actor, _ in service.calls] == [
        PRINCIPAL_IDS[name] for name in EXPECTED_ACTORS
    ]
    for tool_name, _, payload in service.calls[:-2]:
        assert payload["operation_intent"] == tool_name
        assert payload["correlation_id"] == CORRELATION_ID
        assert payload["idempotency_key"]
        assert isinstance(payload["expected_revision"], int)
        assert isinstance(payload["payload"], dict)

    bootstrap = service.calls[0][2]["payload"]
    assert all("definition" in agent for agent in bootstrap["agents"])
    assert all("declared_capabilities" in agent["definition"] for agent in bootstrap["agents"])
    assert "purpose" not in bootstrap["constitution"]
    experiment = service.calls[6][2]["payload"]
    assert experiment["evaluator_principal_id"] == PRINCIPAL_IDS["evaluator"]
    assert experiment["executor_principal_ids"] == [PRINCIPAL_IDS["specialist"]]
    completion = service.calls[11][2]["payload"]
    assert completion["result_evidence_id"]
    assert completion["actual_cost"] == {"currency": "USD", "amount_minor": 500}
    gate = service.calls[14][2]["payload"]
    assert set(gate) == {"action", "stage", "target", "evaluation_id", "amount"}
    assert gate["amount"] == {"currency": "USD", "amount_minor": 9000}
    proposal = service.calls[15][2]["payload"]
    assert proposal["gate_decision_id"]
    assert proposal["ranking"]["formula"] == "exploitation-v1"
    assert proposal["amount"] == gate["amount"]
    assert result["transport"] == "mcp"
    assert result["company_id"] == COMPANY_ID
    assert result["decision_state"] == "committed"
    assert result["audit_event_count"] == len(EXPECTED_TOOLS) - 2
    assert "opaque-runner-test-lease" not in repr(result)


async def test_runner_surfaces_a_bounded_mcp_tool_failure() -> None:
    service = RecordingService(reject="work.claim")

    with pytest.raises(
        VerticalSliceError,
        match=r"work\.claim failed.*runner test rejection",
    ):
        await run_vertical_slice(service, clock=lambda: NOW)


async def test_runner_requires_all_trusted_role_contexts() -> None:
    principals = build_demo_principals()
    principals.pop("allocator")

    with pytest.raises(TypeError, match="missing runner roles: allocator"):
        await run_vertical_slice(RecordingService(), principals, clock=lambda: NOW)


async def test_runner_rejects_a_naive_clock_before_any_mcp_call() -> None:
    service = RecordingService()

    with pytest.raises(ValueError, match="timezone-aware"):
        await run_vertical_slice(service, clock=lambda: datetime(2026, 8, 29))

    assert service.calls == []

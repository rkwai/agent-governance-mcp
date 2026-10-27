"""MCP-to-service Phase 1 vertical-slice integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest
from mcp import StdioServerParameters
from mcp.client import Client

from ai_nature_company.auth import (
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.server import PHASE_ONE_TOOL_NAMES, StaticAuthResolver, build_server
from ai_nature_company.service import MUTATION_TOOLS, READ_TOOLS, create_service

COMPANY_ID = "be685726-a201-4db2-a588-80f50d81e385"
FOUNDER_ID = "b71704b6-47cc-4fd6-a9e0-f2eb51805320"
CORRELATION_ID = "2d79b770-ac39-444c-a40d-e7c90a81de64"


def _founder():
    credential = "integration-founder-credential"
    record = PrincipalRecord(
        FOUNDER_ID,
        (ScopedRoleGrant(Role.FOUNDER, ResourceScope.company(COMPANY_ID)),),
    )
    return LocalTestAuthenticator({credential: record}).authenticate(credential)


def test_mcp_and_service_tool_surfaces_cannot_drift() -> None:
    assert set(PHASE_ONE_TOOL_NAMES) == READ_TOOLS | MUTATION_TOOLS


def _bootstrap_envelope() -> dict[str, object]:
    return {
        "idempotency_key": "bootstrap-v1",
        "expected_revision": 0,
        "correlation_id": CORRELATION_ID,
        "operation_intent": "company.bootstrap",
        "payload": {
            "company_id": COMPANY_ID,
            "name": "AI Nature Company",
            "constitution": {
                "purpose": "Create customer value through verified learning.",
                "objective": "Reach durable risk-adjusted contribution profit.",
                "principles": [
                    "Run bounded experiments.",
                    "Promote only independently verified learning.",
                ],
            },
            "budget": {"limit": "1000.00", "currency": "USD", "duration_days": 90},
            "agents": [
                {
                    "principal_id": FOUNDER_ID,
                    "name": "Founder Gatekeeper",
                    "role": "founder",
                    "capabilities": ["founder.approve_irreversible_action"],
                    "allowed_actions": ["company.bootstrap"],
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_founder_bootstraps_and_reads_company_through_mcp() -> None:
    service = create_service()
    server = build_server(service, StaticAuthResolver(_founder()))

    async with Client(server) as client:
        mutation = await client.call_tool(
            "company.bootstrap",
            {"payload": _bootstrap_envelope()},
        )
        company = await client.call_tool("company.describe", {})

    assert mutation.is_error is False
    assert mutation.structured_content["prior_revision"] == 0
    assert mutation.structured_content["new_revision"] == 1
    assert UUID(mutation.structured_content["operation_id"])
    assert len(mutation.structured_content["event_ids"]) == 3
    assert mutation.structured_content["state"]["company"]["company_id"] == COMPANY_ID

    assert company.is_error is False
    assert company.structured_content["company"]["name"] == "AI Nature Company"
    assert company.structured_content["principal"]["principal_id"] == FOUNDER_ID
    assert "founder" in company.structured_content["principal"]["roles"]


@pytest.mark.asyncio
async def test_stdio_entrypoint_executes_bootstrap_over_the_wire() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_nature_company"],
        cwd=project_root,
        env={
            "AI_NATURE_PRINCIPAL_ID": FOUNDER_ID,
            "AI_NATURE_COMPANY_ID": COMPANY_ID,
            "AI_NATURE_ROLE_GRANTS": '["founder"]',
            "AI_NATURE_LOCAL_MODE": "1",
            "AI_NATURE_DATABASE_URL": "",
            "DATABASE_URL": "",
        },
    )

    async with Client(parameters) as client:
        result = await client.call_tool(
            "company.bootstrap",
            {"payload": _bootstrap_envelope()},
        )

    assert result.is_error is False
    assert result.structured_content["state"]["company"]["company_id"] == COMPANY_ID

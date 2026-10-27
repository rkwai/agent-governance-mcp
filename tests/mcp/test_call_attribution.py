"""Authenticated MCP call-attribution lifecycle contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.client import Client
from mcp.server.auth.provider import AccessToken

from ai_nature_company.attribution import (
    ToolCallAuditEvent,
    ToolCallPhase,
    canonical_sha256,
)
from ai_nature_company.auth import (
    AgentIdentityContext,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.errors import AuthorizationError
from ai_nature_company.server import StaticAuthResolver, build_server
from ai_nature_company.supabase_auth import (
    InMemorySubjectIdentityRepository,
    SubjectIdentityBinding,
    SupabaseIdentityAuthenticator,
)

_ISSUER = "https://project-ref.supabase.co/auth/v1"
_SUBJECT = "10000000-0000-4000-8000-000000000001"
_CLIENT_ID = "50000000-0000-4000-8000-000000000001"
_SESSION_ID = "40000000-0000-4000-8000-000000000001"
_RESOURCE = "https://substrate.example.test/mcp"


def _authority():
    credential = "opaque-local-token"
    record = PrincipalRecord(
        principal_id="principal-cell-a-specialist",
        grants=(
            ScopedRoleGrant(
                Role.SPECIALIST,
                ResourceScope.company("company-1"),
            ),
        ),
        agent_run_id="run-cell-a-specialist-1",
        agent_identity=AgentIdentityContext(
            agent_instance_id="cell-a-specialist-1",
            definition_id="specialist-v1",
            definition_version=1,
            role=Role.SPECIALIST,
            cell_id="cell-a",
        ),
    )
    return LocalTestAuthenticator({credential: record}).authenticate(credential)


async def _supabase_authority():
    auth_subject_id = "auth-subject-cell-a-specialist"
    record = PrincipalRecord(
        principal_id="principal-cell-a-specialist",
        grants=(
            ScopedRoleGrant(
                Role.SPECIALIST,
                ResourceScope.company("company-1"),
            ),
        ),
        agent_run_id="run-cell-a-specialist-1",
        agent_identity=AgentIdentityContext(
            agent_instance_id="cell-a-specialist-1",
            definition_id="specialist-v1",
            definition_version=1,
            role=Role.SPECIALIST,
            cell_id="cell-a",
        ),
        auth_subject_id=auth_subject_id,
    )
    binding = SubjectIdentityBinding(
        issuer=_ISSUER,
        subject=_SUBJECT,
        client_id=_CLIENT_ID,
        principal=record,
        auth_subject_id=auth_subject_id,
        agent_instance_id="cell-a-specialist-1",
        definition_id="specialist-v1",
        definition_version=1,
        cell_id="cell-a",
        assigned_role=Role.SPECIALIST,
        agent_run_id="run-cell-a-specialist-1",
    )
    authenticator = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository([binding]),
        resource_server_url=_RESOURCE,
    )
    return await authenticator(
        AccessToken(
            token="secret-supabase-bearer-token",
            client_id=_CLIENT_ID,
            scopes=["openid"],
            subject=_SUBJECT,
            resource=_RESOURCE,
            claims={
                "iss": _ISSUER,
                "iat": int(datetime.now(UTC).timestamp()),
                "session_id": _SESSION_ID,
            },
        )
    )


class _Service:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls: list[tuple[str, object, dict[str, Any]]] = []

    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((tool_name, auth_context, dict(payload)))
        if self.reject:
            raise AuthorizationError("role cannot allocate")
        return {"accepted": True, "revision": 7}


class _Sink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[ToolCallAuditEvent] = []

    async def record(self, event: ToolCallAuditEvent) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)


class _TerminalFailingSink:
    def __init__(self) -> None:
        self.attempted_phases: list[ToolCallPhase] = []
        self.events: list[ToolCallAuditEvent] = []

    async def record(self, event: ToolCallAuditEvent) -> None:
        self.attempted_phases.append(event.phase)
        if event.phase is not ToolCallPhase.RECEIVED:
            raise RuntimeError("terminal audit unavailable")
        self.events.append(event)


@pytest.mark.asyncio
async def test_successful_call_records_received_and_terminal_attribution() -> None:
    authority = _authority()
    service = _Service()
    sink = _Sink()
    secret = "must-not-enter-audit"
    payload = {
        "cell_id": "cell-a",
        "secret": secret,
        "correlation_id": "correlation-1",
        "idempotency_key": "idempotency-secret",
    }
    server = build_server(
        service,
        StaticAuthResolver(authority),
        call_audit_sink=sink,
    )

    async with Client(server) as client:
        result = await client.call_tool("company.describe", {"payload": payload})

    assert result.is_error is False
    assert [event.phase for event in sink.events] == [
        ToolCallPhase.RECEIVED,
        ToolCallPhase.SUCCEEDED,
    ]
    received, terminal = sink.events
    assert terminal.call_id == received.call_id
    assert received.principal_id == authority.principal_id
    assert received.auth_subject_id is None
    assert received.agent_instance_id == "cell-a-specialist-1"
    assert received.definition_id == "specialist-v1"
    assert received.definition_version == 1
    assert received.role == Role.SPECIALIST.value
    assert received.cell_id == "cell-a"
    assert received.agent_run_id == "run-cell-a-specialist-1"
    assert received.tool_name == "company.describe"
    assert received.arguments_sha256 == canonical_sha256(payload)
    assert received.correlation_id == "correlation-1"
    assert received.idempotency_key_sha256 == canonical_sha256("idempotency-secret")
    assert terminal.response_sha256 == canonical_sha256({"accepted": True, "revision": 7})
    assert terminal.duration_ms is not None and terminal.duration_ms >= 0
    serialized = json.dumps(
        [event.__dict__ if hasattr(event, "__dict__") else str(event) for event in sink.events]
    )
    assert secret not in serialized
    assert "idempotency-secret" not in serialized
    assert "opaque-local-token" not in serialized


@pytest.mark.asyncio
async def test_supabase_auth_subject_is_preserved_in_every_call_lifecycle_event() -> None:
    authority = await _supabase_authority()
    sink = _Sink()
    server = build_server(
        _Service(),
        StaticAuthResolver(authority),
        call_audit_sink=sink,
    )

    async with Client(server) as client:
        result = await client.call_tool("company.describe", {"payload": {}})

    assert result.is_error is False
    assert authority.authentication_method == "supabase-oauth"
    assert authority.auth_subject_id == "auth-subject-cell-a-specialist"
    assert [event.auth_subject_id for event in sink.events] == [
        "auth-subject-cell-a-specialist",
        "auth-subject-cell-a-specialist",
    ]
    assert "secret-supabase-bearer-token" not in repr(sink.events)


@pytest.mark.asyncio
async def test_denied_call_is_attributed_before_returning_the_error() -> None:
    service = _Service(reject=True)
    sink = _Sink()
    server = build_server(
        service,
        StaticAuthResolver(_authority()),
        call_audit_sink=sink,
    )

    async with Client(server) as client:
        result = await client.call_tool("allocation.commit", {"payload": {"cell_id": "cell-a"}})

    assert result.is_error is True
    assert [event.phase for event in sink.events] == [
        ToolCallPhase.RECEIVED,
        ToolCallPhase.DENIED,
    ]
    assert sink.events[-1].error_code == "forbidden"
    assert sink.events[-1].response_sha256 is None


@pytest.mark.asyncio
async def test_received_audit_must_succeed_before_service_dispatch() -> None:
    service = _Service()
    server = build_server(
        service,
        StaticAuthResolver(_authority()),
        call_audit_sink=_Sink(fail=True),
    )

    async with Client(server) as client:
        result = await client.call_tool("company.describe", {"payload": {}})

    assert result.is_error is True
    assert service.calls == []


@pytest.mark.asyncio
async def test_success_terminal_sink_failure_never_attempts_a_contradictory_failure() -> None:
    service = _Service()
    sink = _TerminalFailingSink()
    server = build_server(
        service,
        StaticAuthResolver(_authority()),
        call_audit_sink=sink,
    )

    async with Client(server) as client:
        result = await client.call_tool("company.describe", {"payload": {}})

    assert result.is_error is True
    assert len(service.calls) == 1
    assert sink.attempted_phases == [
        ToolCallPhase.RECEIVED,
        ToolCallPhase.SUCCEEDED,
    ]
    assert [event.phase for event in sink.events] == [ToolCallPhase.RECEIVED]
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "terminal audit unavailable" not in text

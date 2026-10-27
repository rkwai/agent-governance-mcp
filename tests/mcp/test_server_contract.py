"""Contract tests for the Phase 1 MCP transport adapter."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from mcp.client import Client
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context

from ai_nature_company.auth import (
    AgentRunExpiredError,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
)
from ai_nature_company.errors import AuthorizationError, ValidationError
from ai_nature_company.forces import LEDGER_HANDOFF_AFTER_MUTATE
from ai_nature_company.server import (
    MAX_PAYLOAD_BYTES,
    PHASE_ONE_TOOL_NAMES,
    PHASE_ONE_TOOLS,
    SDKAccessTokenResolver,
    StaticAuthResolver,
    build_server,
)


def _authority(
    principal_id: str = "agent-1",
    company_id: str = "company-1",
    role: Role = Role.SPECIALIST,
) -> VerifiedPrincipal:
    credential = "test-credential"
    record = PrincipalRecord(
        principal_id,
        (ScopedRoleGrant(role, ResourceScope.company(company_id)),),
    )
    return LocalTestAuthenticator({credential: record}).authenticate(credential)


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict[str, Any]]] = []

    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        materialized_payload = dict(payload)
        self.calls.append((tool_name, auth_context, materialized_payload))
        return {"tool": tool_name, "payload": materialized_payload}


class _RejectingService:
    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise AuthorizationError("principal lacks the required grant")


class _CrashingService:
    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise RuntimeError("database-password-must-not-cross-the-MCP-boundary")


class _VerboseValidationService:
    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raise ValidationError(
            "1 validation error for Request\n"
            "password\n"
            "  Invalid input [input_value='secret-must-not-cross-boundary']"
        )


class _TypedResultService:
    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "at": datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
            "amount": Decimal("12.50"),
            "id": UUID("96d044eb-45e9-45e2-8882-514285fb8b42"),
        }


class _InvalidResultService:
    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return ["not", "an", "object"]  # type: ignore[return-value]


class _OversizedResultService:
    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {"content": "x" * MAX_PAYLOAD_BYTES}


@dataclass(frozen=True)
class _AsyncResolver:
    authority: VerifiedPrincipal

    async def resolve(self, context: Context[Any, Any]) -> VerifiedPrincipal:
        del context
        return self.authority


class _EmptyResolver:
    def resolve(self, context: Context[Any, Any]) -> None:
        del context
        return None


class _TokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        del token
        return None


@pytest.mark.asyncio
async def test_registers_the_complete_phase_one_surface() -> None:
    server = build_server(
        _RecordingService(),
        StaticAuthResolver(_authority()),
    )

    assert isinstance(server, MCPServer)
    tools = await server.list_tools()

    assert tuple(tool.name for tool in tools) == PHASE_ONE_TOOL_NAMES
    assert len(tools) == len(set(PHASE_ONE_TOOL_NAMES))
    listed = {tool.name: tool.description for tool in tools}
    assert "do not wait for a peer DM" in listed["company.describe"]
    assert "not silence toward founder or other externals" in listed["company.describe"]
    assert "not a peer DM" in listed["protocol.describe"]
    assert "external/host chat, which remains normal" in listed["protocol.describe"]
    for name in (
        "work.create",
        "work.claim",
        "work.complete",
        "work.fail",
        "work.cancel",
        "evidence.append",
        "offer.mutate",
        "experiment.start",
        "cell.transition",
        "cell.amend",
    ):
        assert listed[name].endswith(LEDGER_HANDOFF_AFTER_MUTATE.lstrip())
        assert "Do not DM a peer" in listed[name]
        assert "next force reads MCP" in listed[name]
    specs = {spec.name: spec.description for spec in PHASE_ONE_TOOLS}
    assert specs == listed


@pytest.mark.asyncio
async def test_tool_schemas_do_not_accept_authority_parameters() -> None:
    server = build_server(
        _RecordingService(),
        StaticAuthResolver(_authority()),
    )

    for tool in await server.list_tools():
        properties = tool.input_schema.get("properties", {})
        assert set(properties) <= {"payload"}
        assert not {
            "actor",
            "actor_id",
            "principal",
            "principal_id",
            "role",
            "roles",
            "company",
            "company_id",
            "tenant",
            "tenant_id",
            "scopes",
            "entitlements",
        }.intersection(properties)


@pytest.mark.asyncio
async def test_read_only_annotations_are_advertised_without_open_world_access() -> None:
    server = build_server(
        _RecordingService(),
        StaticAuthResolver(_authority()),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert tools["company.describe"].annotations.read_only_hint is True
    assert tools["company.describe"].annotations.idempotent_hint is True
    assert tools["work.create"].annotations.read_only_hint is False
    assert tools["work.create"].annotations.idempotent_hint is True
    assert all(tool.annotations.open_world_hint is False for tool in tools.values())


@pytest.mark.asyncio
async def test_in_process_client_call_uses_server_resolved_authority() -> None:
    authority = _authority()
    service = _RecordingService()
    server = build_server(service, StaticAuthResolver(authority))
    spoofed_authority = {
        "actor_id": "attacker",
        "role": "founder",
        "company_id": "other-company",
    }

    async with Client(server) as client:
        listed = await client.list_tools()
        result = await client.call_tool("agent.get", {"payload": spoofed_authority})

    assert {tool.name for tool in listed.tools} == set(PHASE_ONE_TOOL_NAMES)
    assert result.is_error is False
    assert result.structured_content == {"tool": "agent.get", "payload": spoofed_authority}
    assert service.calls == [("agent.get", authority, spoofed_authority)]


@pytest.mark.asyncio
async def test_discovery_tool_accepts_an_empty_request() -> None:
    authority = _authority()
    service = _RecordingService()
    server = build_server(service, StaticAuthResolver(authority))

    async with Client(server) as client:
        result = await client.call_tool("company.describe", {})

    assert result.is_error is False
    assert result.structured_content == {"tool": "company.describe", "payload": {}}
    assert service.calls == [("company.describe", authority, {})]


@pytest.mark.asyncio
async def test_injected_server_lifespan_wraps_client_session() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        assert isinstance(server, MCPServer)
        events.append("started")
        yield {"ready": True}
        events.append("stopped")

    server = build_server(
        _RecordingService(),
        StaticAuthResolver(_authority()),
        lifespan=lifespan,
    )

    async with Client(server) as client:
        result = await client.call_tool("company.describe", {})
        assert events == ["started"]

    assert result.is_error is False
    assert events == ["started", "stopped"]


@pytest.mark.asyncio
async def test_company_bootstrap_is_dispatched_through_mcp() -> None:
    authority = _authority("founder-1", role=Role.FOUNDER)
    service = _RecordingService()
    server = build_server(service, StaticAuthResolver(authority))
    payload = {
        "name": "Evolution Works",
        "purpose": "Create customer value through verified learning.",
    }

    async with Client(server) as client:
        result = await client.call_tool("company.bootstrap", {"payload": payload})

    assert result.is_error is False
    assert result.structured_content == {"tool": "company.bootstrap", "payload": payload}
    assert service.calls == [("company.bootstrap", authority, payload)]


@pytest.mark.asyncio
async def test_expected_domain_error_is_a_bounded_tool_error() -> None:
    server = build_server(
        _RejectingService(),
        StaticAuthResolver(_authority()),
    )

    async with Client(server) as client:
        result = await client.call_tool("allocation.commit", {})

    assert result.is_error is True
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "forbidden" in text
    assert "principal lacks the required grant" in text


@pytest.mark.asyncio
async def test_expired_caller_run_is_a_distinct_tool_error() -> None:
    class _ExpiredRunResolver:
        def resolve(self, context: Context[Any, Any]) -> VerifiedPrincipal:
            del context
            raise AgentRunExpiredError(
                "agent run run-1 expired at 2026-10-01T06:59:59Z",
                agent_run_id="run-1",
                expires_at=datetime(2026, 10, 1, 6, 59, 59, tzinfo=UTC),
            )

    server = build_server(_RecordingService(), _ExpiredRunResolver())  # type: ignore[arg-type]

    async with Client(server) as client:
        result = await client.call_tool("company.describe", {})

    assert result.is_error is True
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    compact = text.replace(" ", "")
    assert '"code":"run_expired"' in compact
    assert '"agent_run_id":"run-1"' in compact
    assert "2026-10-01T06:59:59Z" in text
    assert "authentication failed" not in text


@pytest.mark.asyncio
async def test_unexpected_error_does_not_leak_internal_exception_text() -> None:
    server = build_server(
        _CrashingService(),
        StaticAuthResolver(_authority()),
    )

    async with Client(server) as client:
        result = await client.call_tool("allocation.commit", {})

    assert result.is_error is True
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "database-password" not in text
    assert "internal_error" in text
    assert "internal tool error" in text
    assert '{"error":{"code":"internal_error"' in text.replace(" ", "")


@pytest.mark.asyncio
async def test_multiline_validation_error_does_not_echo_rejected_values() -> None:
    server = build_server(
        _VerboseValidationService(),
        StaticAuthResolver(_authority()),
    )

    async with Client(server) as client:
        result = await client.call_tool("offer.register", {})

    text = "\n".join(getattr(item, "text", "") for item in result.content)
    assert result.is_error is True
    assert "1 validation error for Request" in text
    assert "secret-must-not-cross-boundary" not in text
    assert "password" not in text


@pytest.mark.asyncio
async def test_domain_value_types_are_normalized_to_json() -> None:
    server = build_server(
        _TypedResultService(),
        StaticAuthResolver(_authority()),
    )

    async with Client(server) as client:
        result = await client.call_tool("economics.append", {})

    assert result.is_error is False
    assert result.structured_content == {
        "at": "2026-08-29T12:00:00Z",
        "amount": "12.50",
        "id": "96d044eb-45e9-45e2-8882-514285fb8b42",
    }


@pytest.mark.asyncio
async def test_async_auth_resolver_is_supported() -> None:
    authority = _authority()
    service = _RecordingService()
    server = build_server(service, _AsyncResolver(authority))

    async with Client(server) as client:
        result = await client.call_tool("agent.list", {})

    assert result.is_error is False
    assert service.calls == [("agent.list", authority, {})]


@pytest.mark.asyncio
async def test_empty_auth_resolution_fails_closed_before_dispatch() -> None:
    service = _RecordingService()
    server = build_server(service, _EmptyResolver())

    async with Client(server) as client:
        result = await client.call_tool("agent.list", {})

    assert result.is_error is True
    assert "forbidden" in "\n".join(getattr(item, "text", "") for item in result.content)
    assert service.calls == []


@pytest.mark.asyncio
async def test_non_object_service_result_is_a_generic_tool_failure() -> None:
    server = build_server(
        _InvalidResultService(),
        StaticAuthResolver(_authority()),
    )

    async with Client(server) as client:
        result = await client.call_tool("agent.list", {})

    assert result.is_error is True
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    assert "substrate service must return" not in text
    assert "internal_error" in text
    assert "internal tool error" in text


@pytest.mark.asyncio
async def test_oversized_result_is_rejected() -> None:
    server = build_server(
        _OversizedResultService(),
        StaticAuthResolver(_authority()),
    )

    async with Client(server) as client:
        result = await client.call_tool("audit.list", {})

    assert result.is_error is True
    assert "result_too_large" in "\n".join(getattr(item, "text", "") for item in result.content)


@pytest.mark.asyncio
async def test_oversized_payload_is_rejected_before_service_dispatch() -> None:
    service = _RecordingService()
    server = build_server(service, StaticAuthResolver(_authority()))

    async with Client(server) as client:
        result = await client.call_tool(
            "evidence.append",
            {"payload": {"content": "x" * MAX_PAYLOAD_BYTES}},
        )

    assert result.is_error is True
    assert "payload_too_large" in "\n".join(getattr(item, "text", "") for item in result.content)
    assert service.calls == []


@pytest.mark.asyncio
async def test_sdk_token_resolver_uses_verified_sdk_auth_context() -> None:
    token = AccessToken(
        token="opaque-secret",
        client_id="host-client",
        scopes=["mcp:invoke"],
        subject="agent-1",
    )
    authority = _authority()
    observed: list[AccessToken] = []

    def authenticate(access_token: AccessToken) -> VerifiedPrincipal:
        observed.append(access_token)
        return authority

    resolver = SDKAccessTokenResolver(authenticate)
    context_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        resolved = await resolver.resolve(Context())
    finally:
        auth_context_var.reset(context_token)

    assert resolved is authority
    assert observed == [token]


@pytest.mark.asyncio
async def test_sdk_token_resolver_supports_async_authentication() -> None:
    token = AccessToken(token="opaque-secret", client_id="host-client", scopes=[])
    authority = _authority()

    async def authenticate(access_token: AccessToken) -> VerifiedPrincipal:
        assert access_token is token
        return authority

    resolver = SDKAccessTokenResolver(authenticate)
    context_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        resolved = await resolver.resolve(Context())
    finally:
        auth_context_var.reset(context_token)

    assert resolved is authority


@pytest.mark.asyncio
async def test_sdk_token_resolver_rejects_empty_authentication_result() -> None:
    token = AccessToken(token="opaque-secret", client_id="host-client", scopes=[])
    resolver = SDKAccessTokenResolver(lambda access_token: None)
    context_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        with pytest.raises(AuthorizationError, match="did not resolve"):
            await resolver.resolve(Context())
    finally:
        auth_context_var.reset(context_token)


@pytest.mark.asyncio
async def test_sdk_token_resolver_fails_closed_without_authentication() -> None:
    resolver = SDKAccessTokenResolver(lambda token: token)

    with pytest.raises(AuthorizationError, match="authenticated access token required"):
        await resolver.resolve(Context())


def test_sdk_token_resolver_requires_server_auth_configuration() -> None:
    resolver = SDKAccessTokenResolver(lambda token: _authority())

    with pytest.raises(ValueError, match="requires MCP AuthSettings"):
        build_server(_RecordingService(), resolver)

    auth = AuthSettings(
        issuer_url="https://identity.example.test",
        resource_server_url="https://substrate.example.test/mcp",
    )
    server = build_server(
        _RecordingService(),
        resolver,
        auth=auth,
        token_verifier=_TokenVerifier(),
    )

    assert server.settings.auth == auth


def test_build_server_requires_explicit_dependencies() -> None:
    with pytest.raises(TypeError):
        build_server()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="service is required"):
        build_server(None, StaticAuthResolver(_authority()))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="auth_resolver is required"):
        build_server(_RecordingService(), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a VerifiedPrincipal"):
        StaticAuthResolver(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a VerifiedPrincipal"):
        StaticAuthResolver(None)  # type: ignore[arg-type]

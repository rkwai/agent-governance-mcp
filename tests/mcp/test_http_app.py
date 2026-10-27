"""Contracts for the authenticated Streamable HTTP application boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette

from ai_nature_company.auth import (
    AgentIdentityContext,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
)
from ai_nature_company.http_app import HTTPAppSettings, build_http_app
from ai_nature_company.mcp_resource import (
    is_local_mcp_resource_url,
    is_mcp_resource_path_slug,
    request_mcp_resource_url,
    resource_url_for_path,
)
from ai_nature_company.supabase_auth import (
    InMemorySubjectIdentityRepository,
    SubjectIdentityBinding,
    SupabaseIdentityAuthenticator,
)


def _authority(principal_id: str, role: Role) -> VerifiedPrincipal:
    credential = f"credential-{principal_id}"
    record = PrincipalRecord(
        principal_id=principal_id,
        grants=(ScopedRoleGrant(role, ResourceScope.company("company-1")),),
        agent_run_id=f"run-{principal_id}",
    )
    return LocalTestAuthenticator({credential: record}).authenticate(credential)


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, VerifiedPrincipal, dict[str, Any]]] = []

    async def invoke(
        self,
        tool_name: str,
        auth_context: VerifiedPrincipal,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        materialized = dict(payload)
        self.calls.append((tool_name, auth_context, materialized))
        return {
            "principal_id": auth_context.principal_id,
            "agent_run_id": auth_context.agent_run_id,
        }


class _TokenVerifier:
    def __init__(self, tokens: Mapping[str, AccessToken]) -> None:
        self._tokens = dict(tokens)
        self.observed: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.observed.append(token)
        return self._tokens.get(token)


@dataclass(frozen=True)
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


async def _request(
    app: Starlette,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> _Response:
    encoded = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
    request_headers = {"host": "substrate.example.test", **dict(headers or {})}
    if body is not None:
        request_headers.setdefault("content-type", "application/json")
    messages: list[dict[str, Any]] = []
    request_delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(key.encode(), value.encode()) for key, value in request_headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("substrate.example.test", 443),
        "state": {},
    }
    await app(scope, receive, send)  # type: ignore[arg-type]

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode().lower(): value.decode() for key, value in start.get("headers", [])
    }
    return _Response(start["status"], response_headers, response_body)


def _settings(**changes: object) -> HTTPAppSettings:
    values: dict[str, object] = {
        "issuer_url": "https://project.supabase.co/auth/v1",
        "resource_server_url": "https://substrate.example.test/mcp",
        "required_scopes": ("openid",),
        "json_response": True,
    }
    values.update(changes)
    return HTTPAppSettings(**values)  # type: ignore[arg-type]


def _app(
    service: _RecordingService | None = None,
    *,
    verifier: _TokenVerifier | None = None,
    authenticate: object | None = None,
    settings: HTTPAppSettings | None = None,
) -> Starlette:
    authority = _authority("agent-1", Role.SPECIALIST)
    return build_http_app(
        service or _RecordingService(),
        token_verifier=verifier or _TokenVerifier({}),
        authenticate_token=authenticate or (lambda token: authority),  # type: ignore[arg-type]
        settings=settings or _settings(),
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("issuer_url", "", "issuer_url must be a canonical URL"),
        ("issuer_url", "http://project.supabase.co/auth/v1", "issuer_url must use HTTPS"),
        (
            "issuer_url",
            "https://user@project.supabase.co/auth/v1",
            "without user information",
        ),
        (
            "resource_server_url",
            "http://substrate.example.test/mcp",
            "resource_server_url must use HTTPS",
        ),
        (
            "resource_server_url",
            "https://substrate.example.test/",
            "non-root MCP path",
        ),
        (
            "resource_server_url",
            "https://substrate.example.test/mcp?tenant=one",
            "must not contain a query",
        ),
        (
            "resource_server_url",
            "https://substrate.example.test/mcp#fragment",
            "must not contain a fragment",
        ),
        (
            "resource_server_url",
            "https://substrate.example.test/v1/../mcp",
            "canonical MCP path",
        ),
        (
            "resource_server_url",
            "https://substrate.example.test/mcp/",
            "canonical MCP path",
        ),
    ],
)
def test_http_settings_reject_noncanonical_public_urls(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings(**{field: value})


def test_http_settings_derive_the_exact_public_path_and_host() -> None:
    settings = _settings(
        resource_server_url="https://agents.example.test/v1/mcp",
        allowed_hosts=("public-ingress.example.test",),
        service_documentation_url="https://docs.example.test/mcp",
    )

    assert settings.streamable_http_path == "/v1/mcp"
    assert settings.effective_allowed_hosts == ("public-ingress.example.test",)


def test_http_settings_default_allowed_host_to_the_public_resource_authority() -> None:
    settings = _settings(resource_server_url="https://agents.example.test:8443/v1/mcp")

    assert settings.effective_allowed_hosts == ("agents.example.test:8443",)


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"required_scopes": "openid"}, TypeError, "not a string"),
        ({"required_scopes": None}, TypeError, "iterable of strings"),
        ({"required_scopes": ()}, ValueError, "must not be empty"),
        ({"required_scopes": ("",)}, ValueError, "canonical non-empty"),
        ({"required_scopes": ("open\nid",)}, ValueError, "control characters"),
        ({"required_scopes": ("openid", "openid")}, ValueError, "duplicates"),
        ({"stateless_http": 1}, TypeError, "stateless_http must be a boolean"),
        ({"json_response": 1}, TypeError, "json_response must be a boolean"),
        ({"max_request_body_size": True}, TypeError, "must be an integer"),
        ({"max_request_body_size": 0}, ValueError, "must be positive"),
    ],
)
def test_http_settings_reject_ambiguous_transport_configuration(
    changes: Mapping[str, object],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        _settings(**changes)


def test_http_app_requires_both_authentication_dependencies() -> None:
    with pytest.raises(TypeError, match="token_verifier is required"):
        build_http_app(
            _RecordingService(),
            token_verifier=None,  # type: ignore[arg-type]
            authenticate_token=lambda token: _authority("agent-1", Role.SPECIALIST),
            settings=_settings(),
        )
    with pytest.raises(TypeError, match="settings must be HTTPAppSettings"):
        build_http_app(
            _RecordingService(),
            token_verifier=_TokenVerifier({}),
            authenticate_token=lambda token: _authority("agent-1", Role.SPECIALIST),
            settings=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="authenticate_token is required"):
        build_http_app(
            _RecordingService(),
            token_verifier=_TokenVerifier({}),
            authenticate_token=None,  # type: ignore[arg-type]
            settings=_settings(),
        )


@pytest.mark.asyncio
async def test_http_application_lifespan_wraps_the_complete_server_process() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(server: object):
        assert server is not None
        events.append("opened")
        yield {"repository": "ready"}
        events.append("closed")

    authority = _authority("agent-1", Role.SPECIALIST)
    app = build_http_app(
        _RecordingService(),
        token_verifier=_TokenVerifier({}),
        authenticate_token=lambda token: authority,
        settings=_settings(),
        lifespan=lifespan,  # type: ignore[arg-type]
    )

    async with app.router.lifespan_context(app):
        assert events == ["opened"]

    assert events == ["opened", "closed"]


@pytest.mark.asyncio
async def test_rfc_9728_metadata_is_public_and_describes_the_exact_resource() -> None:
    app = _app()

    response = await _request(
        app,
        "GET",
        "/.well-known/oauth-protected-resource/mcp",
    )

    assert response.status == 200
    assert response.json() == {
        "authorization_servers": ["https://project.supabase.co/auth/v1"],
        "bearer_methods_supported": ["header"],
        "resource": "https://substrate.example.test/mcp",
        "scopes_supported": ["openid"],
    }
    assert response.headers["cache-control"] == "public, max-age=3600"


@pytest.mark.asyncio
async def test_unauthenticated_mcp_request_links_to_protected_resource_metadata() -> None:
    app = _app()

    response = await _request(app, "POST", "/mcp", body={})

    assert response.status == 401
    assert response.json()["error"] == "invalid_token"
    assert (
        'resource_metadata="https://substrate.example.test/'
        '.well-known/oauth-protected-resource/mcp"' in response.headers["www-authenticate"]
    )


@pytest.mark.asyncio
async def test_invalid_bearer_token_fails_before_the_mcp_transport() -> None:
    verifier = _TokenVerifier({})
    app = _app(verifier=verifier)

    response = await _request(
        app,
        "POST",
        "/mcp",
        body={},
        headers={"authorization": "Bearer invalid"},
    )

    assert response.status == 401
    assert verifier.observed == ["invalid"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"host": "untrusted.example.test"}, 421),
        ({"origin": "https://untrusted.example.test"}, 403),
    ],
)
async def test_streamable_transport_rejects_untrusted_host_and_origin(
    headers: Mapping[str, str],
    expected_status: int,
) -> None:
    verifier = _TokenVerifier(
        {
            "valid": AccessToken(
                token="valid",
                client_id="grok-connector",
                scopes=["openid"],
                subject="supabase-agent-sub",
            )
        }
    )
    app = _app(verifier=verifier)

    async with app.router.lifespan_context(app):
        response = await _request(
            app,
            "POST",
            "/mcp",
            body={},
            headers={"authorization": "Bearer valid", **headers},
        )

    assert response.status == expected_status


@pytest.mark.asyncio
async def test_streamable_transport_rejects_an_oversized_raw_request() -> None:
    verifier = _TokenVerifier(
        {
            "valid": AccessToken(
                token="valid",
                client_id="grok-connector",
                scopes=["openid"],
                subject="supabase-agent-sub",
            )
        }
    )
    app = _app(verifier=verifier, settings=_settings(max_request_body_size=32))

    async with app.router.lifespan_context(app):
        response = await _request(
            app,
            "POST",
            "/mcp",
            body={"payload": "x" * 100},
            headers={"authorization": "Bearer valid"},
        )

    assert response.status == 413


@pytest.mark.asyncio
async def test_streamable_http_attributes_two_tokens_to_two_fixed_principals() -> None:
    owner = _authority("cell-a-owner", Role.CELL_OWNER)
    evaluator = _authority("cell-a-evaluator", Role.EVALUATOR)
    verifier = _TokenVerifier(
        {
            "owner-token": AccessToken(
                token="owner-token",
                client_id="grok-connector",
                scopes=["openid"],
                subject="supabase-owner-sub",
            ),
            "evaluator-token": AccessToken(
                token="evaluator-token",
                client_id="grok-connector",
                scopes=["openid"],
                subject="supabase-evaluator-sub",
            ),
        }
    )
    principals = {
        "supabase-owner-sub": owner,
        "supabase-evaluator-sub": evaluator,
    }
    resolved_subjects: list[str] = []

    def authenticate_token(token: AccessToken) -> VerifiedPrincipal:
        assert token.subject is not None
        resolved_subjects.append(token.subject)
        return principals[token.subject]

    service = _RecordingService()
    app = _app(service, verifier=verifier, authenticate=authenticate_token)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "grok-test", "version": "1"},
        },
    }
    spoofed = {
        "principal_id": "founder-spoof",
        "role": "founder",
        "company_id": "other-company",
    }

    async with app.router.lifespan_context(app):
        initialized = await _request(
            app,
            "POST",
            "/mcp",
            body=initialize,
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer owner-token",
            },
        )
        owner_call = await _request(
            app,
            "POST",
            "/mcp",
            body={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "company.describe",
                    "arguments": {"payload": spoofed},
                },
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer owner-token",
            },
        )
        evaluator_call = await _request(
            app,
            "POST",
            "/mcp",
            body={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "company.describe", "arguments": {}},
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer evaluator-token",
            },
        )

    assert initialized.status == 200
    assert owner_call.status == 200
    assert owner_call.json()["result"]["structuredContent"] == {
        "agent_run_id": "run-cell-a-owner",
        "principal_id": "cell-a-owner",
    }
    assert evaluator_call.status == 200
    assert evaluator_call.json()["result"]["structuredContent"] == {
        "agent_run_id": "run-cell-a-evaluator",
        "principal_id": "cell-a-evaluator",
    }
    assert resolved_subjects == ["supabase-owner-sub", "supabase-evaluator-sub"]
    assert service.calls == [
        ("company.describe", owner, spoofed),
        ("company.describe", evaluator, {}),
    ]


@pytest.mark.asyncio
async def test_required_scope_is_enforced_before_principal_resolution() -> None:
    verifier = _TokenVerifier(
        {
            "wrong-scope": AccessToken(
                token="wrong-scope",
                client_id="grok-connector",
                scopes=["profile"],
                subject="supabase-agent-sub",
            )
        }
    )
    resolved: list[AccessToken] = []
    app = _app(
        verifier=verifier,
        authenticate=lambda token: resolved.append(token),
    )

    response = await _request(
        app,
        "POST",
        "/mcp",
        body={},
        headers={"authorization": "Bearer wrong-scope"},
    )

    assert response.status == 403
    assert response.json()["error"] == "insufficient_scope"
    assert resolved == []


@pytest.mark.parametrize(
    "slug",
    ["operating-specialist-1", "a", "cell-owner-2", "eval1"],
)
def test_conservative_path_slugs_are_accepted(slug: str) -> None:
    assert is_mcp_resource_path_slug(slug) is True


@pytest.mark.parametrize(
    "slug",
    ["", ".", "..", "Foo", "foo_bar", "foo/bar", "-foo", "foo-", "foo--bar", "a" * 65],
)
def test_nonconservative_path_slugs_are_rejected(slug: str) -> None:
    assert is_mcp_resource_path_slug(slug) is False


def test_local_resource_url_accepts_same_origin_path_alias_only() -> None:
    primary = "https://substrate.example.test/mcp"
    assert is_local_mcp_resource_url(primary, primary) is True
    assert (
        is_local_mcp_resource_url(
            primary, "https://substrate.example.test/mcp/operating-specialist-1"
        )
        is True
    )
    assert is_local_mcp_resource_url(primary, "https://other.example.test/mcp") is False
    assert is_local_mcp_resource_url(primary, "https://substrate.example.test/mcp/../etc") is False
    assert is_local_mcp_resource_url(primary, "https://substrate.example.test/mcp/") is False
    assert resource_url_for_path(primary, "/mcp/operating-specialist-1") == (
        "https://substrate.example.test/mcp/operating-specialist-1"
    )
    assert resource_url_for_path(primary, "/mcp/../etc") is None
    assert resource_url_for_path(primary, "/mcp/") is None


@pytest.mark.asyncio
async def test_rfc_9728_metadata_for_a_path_alias_names_that_resource() -> None:
    app = _app()

    response = await _request(
        app,
        "GET",
        "/.well-known/oauth-protected-resource/mcp/operating-specialist-1",
    )

    assert response.status == 200
    assert response.json() == {
        "authorization_servers": ["https://project.supabase.co/auth/v1"],
        "bearer_methods_supported": ["header"],
        "resource": "https://substrate.example.test/mcp/operating-specialist-1",
        "scopes_supported": ["openid"],
    }
    assert response.headers["cache-control"] == "public, max-age=3600"


@pytest.mark.asyncio
async def test_unauthenticated_path_alias_links_to_that_resource_metadata() -> None:
    app = _app()

    response = await _request(app, "POST", "/mcp/operating-specialist-1", body={})

    assert response.status == 401
    assert response.json()["error"] == "invalid_token"
    assert (
        'resource_metadata="https://substrate.example.test/'
        '.well-known/oauth-protected-resource/mcp/operating-specialist-1"'
        in response.headers["www-authenticate"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/mcp/", 307),
        ("/mcp/..", 404),
        ("/mcp/../etc", 404),
        ("/mcp/Foo", 404),
        ("/mcp/foo_bar", 404),
        ("/mcp/-missing", 404),
        ("/mcp/trailing-", 404),
        ("/.well-known/oauth-protected-resource/mcp/..", 404),
        ("/.well-known/oauth-protected-resource/mcp/Foo", 404),
    ],
)
async def test_invalid_or_empty_path_alias_fails_closed(path: str, expected_status: int) -> None:
    app = _app()

    response = await _request(app, "GET" if "well-known" in path else "POST", path, body={})

    assert response.status == expected_status
    if expected_status == 307:
        assert response.headers["location"].endswith("/mcp")


@pytest.mark.asyncio
async def test_streamable_http_serves_mcp_on_a_path_alias() -> None:
    owner = _authority("cell-a-owner", Role.CELL_OWNER)
    verifier = _TokenVerifier(
        {
            "owner-token": AccessToken(
                token="owner-token",
                client_id="grok-connector",
                scopes=["openid"],
                subject="supabase-owner-sub",
                resource="https://substrate.example.test/mcp",
            )
        }
    )
    service = _RecordingService()
    app = _app(service, verifier=verifier, authenticate=lambda token: owner)

    async with app.router.lifespan_context(app):
        initialized = await _request(
            app,
            "POST",
            "/mcp/operating-specialist-1",
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "grok-test", "version": "1"},
                },
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer owner-token",
            },
        )
        call = await _request(
            app,
            "POST",
            "/mcp/operating-specialist-1",
            body={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "company.describe", "arguments": {}},
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer owner-token",
            },
        )
        primary = await _request(
            app,
            "POST",
            "/mcp",
            body={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "company.describe", "arguments": {}},
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer owner-token",
            },
        )

    assert initialized.status == 200
    assert call.status == 200
    assert call.json()["result"]["structuredContent"] == {
        "agent_run_id": "run-cell-a-owner",
        "principal_id": "cell-a-owner",
    }
    assert primary.status == 200
    assert service.calls == [
        ("company.describe", owner, {}),
        ("company.describe", owner, {}),
    ]


@pytest.mark.asyncio
async def test_path_alias_host_allowlist_stays_host_based() -> None:
    verifier = _TokenVerifier(
        {
            "valid": AccessToken(
                token="valid",
                client_id="grok-connector",
                scopes=["openid"],
                subject="supabase-agent-sub",
            )
        }
    )
    app = _app(verifier=verifier)

    async with app.router.lifespan_context(app):
        rejected = await _request(
            app,
            "POST",
            "/mcp/operating-specialist-1",
            body={},
            headers={
                "authorization": "Bearer valid",
                "host": "operating-specialist-1",
            },
        )
        accepted_host = await _request(
            app,
            "POST",
            "/mcp/operating-specialist-1",
            body={},
            headers={"authorization": "Bearer valid"},
        )

    assert rejected.status == 421
    assert accepted_host.status != 421
    assert accepted_host.status != 404


@pytest.mark.asyncio
async def test_identity_resolution_uses_the_request_path_resource_url() -> None:
    identity = AgentIdentityContext(
        agent_instance_id="instance-cell-a-owner",
        definition_id="definition-cell-owner",
        definition_version=1,
        role=Role.CELL_OWNER,
        cell_id="cell-1",
    )
    record = PrincipalRecord(
        principal_id="cell-a-owner",
        grants=(ScopedRoleGrant(Role.CELL_OWNER, ResourceScope.company("company-1")),),
        agent_run_id="run-cell-a-owner",
        agent_identity=identity,
        auth_subject_id="auth-cell-a-owner",
    )
    binding = SubjectIdentityBinding(
        issuer="https://project.supabase.co/auth/v1",
        subject="supabase-owner-sub",
        client_id="grok-connector",
        principal=record,
        auth_subject_id="auth-cell-a-owner",
        agent_instance_id=identity.agent_instance_id,
        definition_id=identity.definition_id,
        definition_version=identity.definition_version,
        cell_id=identity.cell_id,
        assigned_role=identity.role,
        agent_run_id="run-cell-a-owner",
    )
    seen: list[str] = []

    class _RecordingRepository(InMemorySubjectIdentityRepository):
        async def resolve(self, **kwargs: object):  # type: ignore[override]
            resource = kwargs["resource_server_url"]
            assert isinstance(resource, str)
            seen.append(resource)
            assert request_mcp_resource_url() == resource
            return await super().resolve(**kwargs)

    authenticator = SupabaseIdentityAuthenticator(
        _RecordingRepository([binding]),
        resource_server_url="https://substrate.example.test/mcp",
    )
    verifier = _TokenVerifier(
        {
            "owner-token": AccessToken(
                token="owner-token",
                client_id="grok-connector",
                scopes=["openid"],
                subject="supabase-owner-sub",
                resource="https://substrate.example.test/mcp",
                claims={
                    "iss": "https://project.supabase.co/auth/v1",
                    "aud": "authenticated",
                    "iat": 1,
                    "session_id": "40000000-0000-4000-8000-000000000001",
                },
            )
        }
    )
    app = _app(_RecordingService(), verifier=verifier, authenticate=authenticator)

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "grok-test", "version": "1"},
        },
    }
    describe = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "company.describe", "arguments": {}},
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "authorization": "Bearer owner-token",
    }

    async with app.router.lifespan_context(app):
        await _request(
            app,
            "POST",
            "/mcp/operating-specialist-1",
            body=initialize,
            headers=headers,
        )
        path_call = await _request(
            app,
            "POST",
            "/mcp/operating-specialist-1",
            body=describe,
            headers=headers,
        )
        await _request(app, "POST", "/mcp", body=initialize, headers=headers)
        primary_call = await _request(app, "POST", "/mcp", body=describe, headers=headers)

    assert path_call.status == 200
    assert primary_call.status == 200
    assert seen == [
        "https://substrate.example.test/mcp/operating-specialist-1",
        "https://substrate.example.test/mcp",
    ]

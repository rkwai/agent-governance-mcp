"""Hosted MCP identity, authorization, and attribution end-to-end contract.

The test crosses the real ASGI Streamable HTTP transport and deterministic
substrate service.  Only Supabase's remote JWKS lookup and PostgreSQL durability
are replaced with local seams; each of those adapters has its own focused and
live-integration contracts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette

from ai_nature_company.attribution import ToolCallAuditEvent, ToolCallPhase
from ai_nature_company.auth import (
    AgentIdentityContext,
    PrincipalRecord,
    ResourceScope,
    ResourceType,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.http_app import HTTPAppSettings, build_http_app
from ai_nature_company.supabase_auth import (
    InMemorySubjectIdentityRepository,
    SubjectIdentityBinding,
    SupabaseIdentityAuthenticator,
    SupabaseJWTSettings,
    SupabaseJWTVerifier,
)
from tests.acceptance.scenario import Scenario
from tests.acceptance.support import (
    CELL_ID,
    COMPANY_ID,
    PRINCIPAL_IDS,
    RUN_IDS,
    load_definition,
    stable_uuid,
)

ISSUER = "https://project-ref.supabase.co/auth/v1"
AUDIENCE = "authenticated"
RESOURCE_SERVER = "https://substrate.example.test/mcp"
CLIENT_ID = "grok-cell-owner-connector"
OTHER_CLIENT_ID = "grok-other-connector"
SUBJECT = stable_uuid("supabase-subject-owner")
AUTH_SUBJECT_ID = stable_uuid("auth-subject-owner")
OWNER_DEFINITION_ID = "10000000-0000-4000-8000-000000000001"
OTHER_CELL_ID = stable_uuid("other-cell")


class _StaticSigningKeys:
    def __init__(self, public_key: object) -> None:
        self.public_key = public_key

    async def signing_key(self, raw_token: str) -> object:
        assert raw_token
        return self.public_key


class _AppendOnlyAuditSink:
    """Strict local journal for the combined boundary test.

    The production sink commits these events to PostgreSQL.  This seam still
    enforces the important dispatch invariant: one received record must exist
    before exactly one terminal record can be appended for a call.
    """

    def __init__(self) -> None:
        self.events: list[ToolCallAuditEvent] = []
        self._received: dict[str, ToolCallAuditEvent] = {}
        self._terminal: set[str] = set()

    async def record(self, event: ToolCallAuditEvent) -> None:
        if event.phase is ToolCallPhase.RECEIVED:
            assert event.call_id not in self._received
            self._received[event.call_id] = event
        else:
            received = self._received[event.call_id]
            assert event.call_id not in self._terminal
            assert event.principal_id == received.principal_id
            assert event.authentication_id == received.authentication_id
            assert event.arguments_sha256 == received.arguments_sha256
            self._terminal.add(event.call_id)
        self.events.append(event)


async def _request(
    app: Starlette,
    body: Mapping[str, Any],
    *,
    raw_token: str,
) -> tuple[int, dict[str, Any]]:
    encoded = json.dumps(body, separators=(",", ":")).encode()
    messages: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = {
        "host": "substrate.example.test",
        "accept": "application/json, text/event-stream",
        "authorization": f"Bearer {raw_token}",
        "content-type": "application/json",
    }
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("substrate.example.test", 443),
        "state": {},
    }
    await app(scope, receive, send)  # type: ignore[arg-type]

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(response_body)


def _raw_token(signing_key: object, *, client_id: str = CLIENT_ID) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": SUBJECT,
            "client_id": client_id,
            "session_id": str(uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
            "scope": "openid",
            # These claims are attacker-controlled and must never become authority.
            "principal_id": PRINCIPAL_IDS["founder"],
            "role": Role.FOUNDER.value,
            "company_id": stable_uuid("attacker-company"),
            "cell_id": OTHER_CELL_ID,
            "agent_instance_id": stable_uuid("attacker-instance"),
            "agent_run_id": RUN_IDS["evaluator"],
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": "local-full-stack-key"},
    )


def _binding() -> SubjectIdentityBinding:
    identity = AgentIdentityContext(
        agent_instance_id=OWNER_DEFINITION_ID,
        definition_id=OWNER_DEFINITION_ID,
        definition_version=1,
        role=Role.CELL_OWNER,
        cell_id=CELL_ID,
    )
    principal = PrincipalRecord(
        principal_id=PRINCIPAL_IDS["owner"],
        grants=(
            ScopedRoleGrant(
                Role.CELL_OWNER,
                ResourceScope.resource(COMPANY_ID, ResourceType.CELL, CELL_ID),
            ),
        ),
        agent_run_id=RUN_IDS["owner"],
        agent_identity=identity,
        auth_subject_id=AUTH_SUBJECT_ID,
    )
    return SubjectIdentityBinding(
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
        principal=principal,
        auth_subject_id=AUTH_SUBJECT_ID,
        agent_instance_id=identity.agent_instance_id,
        definition_id=identity.definition_id,
        definition_version=identity.definition_version,
        cell_id=identity.cell_id,
        assigned_role=identity.role,
        agent_run_id=RUN_IDS["owner"],
    )


@pytest.mark.asyncio
async def test_hosted_http_uses_fixed_agent_identity_for_success_and_denial() -> None:
    scenario = Scenario()
    await scenario.bootstrap()

    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE_SERVER,
            allowed_client_ids=frozenset({CLIENT_ID, OTHER_CLIENT_ID}),
        ),
        signing_keys=_StaticSigningKeys(signing_key.public_key()),
    )
    authenticate = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository([_binding()]),
        resource_server_url=RESOURCE_SERVER,
    )
    audit = _AppendOnlyAuditSink()
    app = build_http_app(
        scenario.service,
        token_verifier=verifier,
        authenticate_token=authenticate,
        settings=HTTPAppSettings(
            issuer_url=ISSUER,
            resource_server_url=RESOURCE_SERVER,
        ),
        call_audit_sink=audit,
    )
    raw_token = _raw_token(signing_key)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "grok-full-stack-test", "version": "1"},
        },
    }
    spoofed_authority = {
        "principal_id": PRINCIPAL_IDS["founder"],
        "role": Role.FOUNDER.value,
        "cell_id": OTHER_CELL_ID,
        "agent_run_id": RUN_IDS["evaluator"],
        "agent_instance_id": stable_uuid("attacker-instance"),
        "definition_id": stable_uuid("attacker-definition"),
        "secret": "request-content-must-not-enter-audit",
    }

    async with app.router.lifespan_context(app):
        initialized_status, _ = await _request(app, initialize, raw_token=raw_token)
        describe_status, describe = await _request(
            app,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "company.describe",
                    "arguments": {"payload": spoofed_authority},
                },
            },
            raw_token=raw_token,
        )
        denied_status, denied = await _request(
            app,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "cell.get",
                    "arguments": {
                        "payload": {
                            "cell_id": OTHER_CELL_ID,
                            **spoofed_authority,
                        }
                    },
                },
            },
            raw_token=raw_token,
        )
        wrong_client_status, wrong_client = await _request(
            app,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "company.describe", "arguments": {}},
            },
            raw_token=_raw_token(signing_key, client_id=OTHER_CLIENT_ID),
        )

    assert initialized_status == 200
    assert describe_status == 200
    principal = describe["result"]["structuredContent"]["principal"]
    assert principal == {
        "principal_id": PRINCIPAL_IDS["owner"],
        "roles": [Role.CELL_OWNER.value],
        "effective_capabilities": sorted(load_definition("cell-owner")["declared_capabilities"]),
        "agent_instance_id": OWNER_DEFINITION_ID,
        "definition_id": OWNER_DEFINITION_ID,
        "definition_version": 1,
        "role": Role.CELL_OWNER.value,
        "cell_id": CELL_ID,
        "agent_run_id": RUN_IDS["owner"],
    }
    assert denied_status == 200
    assert denied["result"]["isError"] is True
    assert wrong_client_status == 200
    assert wrong_client["result"]["isError"] is True

    assert [event.phase for event in audit.events] == [
        ToolCallPhase.RECEIVED,
        ToolCallPhase.SUCCEEDED,
        ToolCallPhase.RECEIVED,
        ToolCallPhase.DENIED,
    ]
    for event in audit.events:
        assert event.principal_id == PRINCIPAL_IDS["owner"]
        assert event.auth_subject_id == AUTH_SUBJECT_ID
        assert event.agent_instance_id == OWNER_DEFINITION_ID
        assert event.definition_id == OWNER_DEFINITION_ID
        assert event.definition_version == 1
        assert event.role == Role.CELL_OWNER.value
        assert event.cell_id == CELL_ID
        assert event.agent_run_id == RUN_IDS["owner"]
    assert audit.events[-1].error_code == "forbidden"
    assert raw_token not in repr(audit.events)
    assert spoofed_authority["secret"] not in repr(audit.events)

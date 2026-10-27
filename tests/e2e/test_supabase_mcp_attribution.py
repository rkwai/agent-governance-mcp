"""Fake-only Supabase identity-to-MCP call-attribution contracts.

These tests deliberately use an ephemeral signing key, an in-memory subject
repository, the in-process MCP transport, and an in-memory attribution sink.  They
exercise the same trust boundaries as production without contacting Supabase.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.settings import AuthSettings

from ai_nature_company.auth import (
    AgentIdentityContext,
    PrincipalRecord,
    ResourceScope,
    ResourceType,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
)
from ai_nature_company.errors import AuthorizationError
from ai_nature_company.policy import AuthorizationPolicy, Capability
from ai_nature_company.server import SDKAccessTokenResolver, build_server
from ai_nature_company.supabase_auth import (
    InMemorySubjectIdentityRepository,
    SubjectIdentityBinding,
    SupabaseIdentityAuthenticator,
    SupabaseJWTSettings,
    SupabaseJWTVerifier,
)

ISSUER = "https://project-ref.supabase.co/auth/v1"
AUDIENCE = "authenticated"
RESOURCE_SERVER = "https://substrate.example.test/mcp"
CLIENT_ID = "grok-shared-connector"
COMPANY_ID = "company-1"
CELL_A = "cell-a"
CELL_B = "cell-b"
OWNER_SUBJECT = "subject-cell-a-owner"
EVALUATOR_SUBJECT = "subject-cell-a-evaluator"
OWNER_PRINCIPAL = "principal-cell-a-owner"
EVALUATOR_PRINCIPAL = "principal-cell-a-evaluator"
OWNER_RUN = "run-cell-a-owner-1"
EVALUATOR_RUN = "run-cell-a-evaluator-1"


class _StaticSigningKeys:
    """Local signing-key provider with the production provider's async seam."""

    def __init__(self, public_key: object) -> None:
        self.public_key = public_key

    async def signing_key(self, raw_token: str) -> object:
        assert raw_token
        return self.public_key


@dataclass(frozen=True, slots=True)
class _BoundInstance:
    """Server-owned agent-instance metadata used by the fake audit sink."""

    principal_id: str
    agent_instance_id: str
    definition_id: str
    definition_version: int
    role: Role
    cell_id: str | None


class _AttributionContractService:
    """Reference call-attribution wrapper around the real authorization policy.

    Production persistence may use a middleware or service decorator.  The contract
    is intentionally independent of that choice: identity metadata comes from a
    trusted binding, payloads are reduced to a digest, and denied calls are recorded
    before a caller-visible error is raised.
    """

    def __init__(self, bindings: list[SubjectIdentityBinding]) -> None:
        self.policy = AuthorizationPolicy()
        self.instances = {
            binding.principal.principal_id: _BoundInstance(
                principal_id=binding.principal.principal_id,
                agent_instance_id=binding.agent_instance_id,
                definition_id=binding.definition_id,
                definition_version=binding.definition_version,
                role=binding.assigned_role,
                cell_id=binding.cell_id,
            )
            for binding in bindings
        }
        self.audits: list[dict[str, Any]] = []

    async def invoke(
        self,
        tool_name: str,
        auth_context: VerifiedPrincipal,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        instance = self.instances[auth_context.principal_id]
        cell_id = str(payload.get("cell_id", instance.cell_id or ""))
        resource = ResourceScope.resource(
            COMPANY_ID,
            ResourceType.CELL,
            cell_id,
        )
        capability = {
            "cell.get": Capability.CELL_GET,
            "allocation.commit": Capability.ALLOCATION_COMMIT_BOUNDED,
        }[tool_name]
        decision = self.policy.decide(auth_context, capability, resource)
        payload_digest = hashlib.sha256(
            json.dumps(
                dict(payload),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        call_id = str(uuid4())
        common_audit = {
            "call_id": call_id,
            "request_id": str(uuid4()),
            "principal_id": auth_context.principal_id,
            "auth_subject_id": auth_context.auth_subject_id,
            "authentication_id": auth_context.authentication_id,
            "agent_instance_id": instance.agent_instance_id,
            "definition_id": instance.definition_id,
            "definition_version": instance.definition_version,
            "role": instance.role.value,
            "cell_id": instance.cell_id,
            "agent_run_id": auth_context.agent_run_id,
            "tool_name": tool_name,
            "arguments_sha256": payload_digest,
            "resource_type": ResourceType.CELL.value,
            "resource_id": cell_id,
        }
        self.audits.append(
            {
                **common_audit,
                "phase": "received",
                "authorization_outcome": "pending",
                "authorization_reason_code": None,
                "execution_outcome": "pending",
                "resource_revision": None,
            }
        )
        terminal_audit = {
            **common_audit,
            "phase": "succeeded" if decision.allowed else "denied",
            "authorization_outcome": "allowed" if decision.allowed else "denied",
            "authorization_reason_code": decision.reason,
            "execution_outcome": "succeeded" if decision.allowed else "not_executed",
            "resource_revision": 7 if decision.allowed else None,
        }
        self.audits.append(terminal_audit)
        if not decision.allowed:
            raise AuthorizationError(f"call denied: {decision.reason}")
        return {
            "cell": {"cell_id": cell_id, "revision": 7},
            "principal_id": auth_context.principal_id,
        }


def _terminal_audits(service: _AttributionContractService) -> list[dict[str, Any]]:
    return [audit for audit in service.audits if audit["phase"] != "received"]


@pytest.fixture(scope="module")
def signing_key() -> object:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _record(
    principal_id: str,
    role: Role,
    *,
    cell_id: str,
    agent_run_id: str,
) -> PrincipalRecord:
    agent_identity = AgentIdentityContext(
        agent_instance_id=f"instance-{principal_id}",
        definition_id=f"definition-{role.value}",
        definition_version=1,
        role=role,
        cell_id=cell_id,
    )
    return PrincipalRecord(
        principal_id=principal_id,
        grants=(
            ScopedRoleGrant(
                role,
                ResourceScope.resource(COMPANY_ID, ResourceType.CELL, cell_id),
            ),
        ),
        agent_run_id=agent_run_id,
        agent_identity=agent_identity,
        auth_subject_id=f"auth-{principal_id}",
    )


def _binding(
    *,
    subject: str,
    principal_id: str,
    role: Role,
    agent_run_id: str,
) -> SubjectIdentityBinding:
    principal = _record(
        principal_id,
        role,
        cell_id=CELL_A,
        agent_run_id=agent_run_id,
    )
    assert principal.auth_subject_id is not None
    return SubjectIdentityBinding(
        issuer=ISSUER,
        subject=subject,
        client_id=CLIENT_ID,
        principal=principal,
        auth_subject_id=principal.auth_subject_id,
        agent_instance_id=f"instance-{principal_id}",
        definition_id=f"definition-{role.value}",
        definition_version=1,
        cell_id=CELL_A,
        assigned_role=role,
        agent_run_id=agent_run_id,
    )


@pytest.fixture
def bindings() -> list[SubjectIdentityBinding]:
    return [
        _binding(
            subject=OWNER_SUBJECT,
            principal_id=OWNER_PRINCIPAL,
            role=Role.CELL_OWNER,
            agent_run_id=OWNER_RUN,
        ),
        _binding(
            subject=EVALUATOR_SUBJECT,
            principal_id=EVALUATOR_PRINCIPAL,
            role=Role.EVALUATOR,
            agent_run_id=EVALUATOR_RUN,
        ),
    ]


def _raw_token(
    signing_key: object,
    *,
    subject: str,
    client_id: str = CLIENT_ID,
    extra_claims: Mapping[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "client_id": client_id,
        "session_id": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "scope": "openid",
    }
    claims.update(extra_claims or {})
    return jwt.encode(
        claims,
        signing_key,
        algorithm="RS256",
        headers={"kid": "local-e2e-key"},
    )


def _stack(
    signing_key: object,
    bindings: list[SubjectIdentityBinding],
) -> tuple[SupabaseJWTVerifier, _AttributionContractService, object]:
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE_SERVER,
            allowed_client_ids=frozenset({CLIENT_ID}),
        ),
        signing_keys=_StaticSigningKeys(signing_key.public_key()),  # type: ignore[attr-defined]
    )
    authenticator = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository(bindings),
        resource_server_url=RESOURCE_SERVER,
    )
    service = _AttributionContractService(bindings)
    server = build_server(
        service,
        SDKAccessTokenResolver(authenticator),
        auth=AuthSettings(
            issuer_url=ISSUER,
            resource_server_url=RESOURCE_SERVER,
            required_scopes=["openid"],
        ),
        token_verifier=verifier,
    )
    return verifier, service, server


async def _call_with_token(
    verifier: SupabaseJWTVerifier,
    server: object,
    raw_token: str,
    tool_name: str,
    payload: Mapping[str, Any],
) -> object:
    access_token = await verifier.verify_token(raw_token)
    assert access_token is not None
    context_token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        async with Client(server) as client:  # type: ignore[arg-type]
            return await client.call_tool(tool_name, {"payload": dict(payload)})
    finally:
        auth_context_var.reset(context_token)


@pytest.mark.asyncio
async def test_one_connector_resolves_distinct_agent_identities_and_attributes_reads(
    signing_key: object,
    bindings: list[SubjectIdentityBinding],
) -> None:
    verifier, service, server = _stack(signing_key, bindings)

    owner_result = await _call_with_token(
        verifier,
        server,
        _raw_token(signing_key, subject=OWNER_SUBJECT),
        "cell.get",
        {"cell_id": CELL_A},
    )
    evaluator_result = await _call_with_token(
        verifier,
        server,
        _raw_token(signing_key, subject=EVALUATOR_SUBJECT),
        "cell.get",
        {"cell_id": CELL_A},
    )

    assert owner_result.is_error is False  # type: ignore[attr-defined]
    assert evaluator_result.is_error is False  # type: ignore[attr-defined]
    terminal = _terminal_audits(service)
    assert [audit["principal_id"] for audit in terminal] == [
        OWNER_PRINCIPAL,
        EVALUATOR_PRINCIPAL,
    ]
    assert [audit["agent_run_id"] for audit in terminal] == [
        OWNER_RUN,
        EVALUATOR_RUN,
    ]
    assert [audit["auth_subject_id"] for audit in terminal] == [
        f"auth-{OWNER_PRINCIPAL}",
        f"auth-{EVALUATOR_PRINCIPAL}",
    ]
    assert len({audit["authentication_id"] for audit in terminal}) == 2
    assert all(audit["phase"] == "succeeded" for audit in terminal)
    assert all(audit["authorization_outcome"] == "allowed" for audit in terminal)
    assert all(audit["execution_outcome"] == "succeeded" for audit in terminal)
    assert all(audit["resource_revision"] == 7 for audit in terminal)
    assert all(audit["tool_name"] == "cell.get" for audit in terminal)


@pytest.mark.asyncio
async def test_jwt_and_payload_authority_claims_cannot_change_server_binding(
    signing_key: object,
    bindings: list[SubjectIdentityBinding],
) -> None:
    verifier, service, server = _stack(signing_key, bindings)
    raw_token = _raw_token(
        signing_key,
        subject=OWNER_SUBJECT,
        extra_claims={
            "role": "founder",
            "principal_id": EVALUATOR_PRINCIPAL,
            "company_id": "attacker-company",
            "cell_id": CELL_B,
            "agent_run_id": EVALUATOR_RUN,
        },
    )

    result = await _call_with_token(
        verifier,
        server,
        raw_token,
        "cell.get",
        {
            "cell_id": CELL_A,
            "role": "founder",
            "principal_id": EVALUATOR_PRINCIPAL,
            "agent_run_id": EVALUATOR_RUN,
        },
    )

    assert result.is_error is False  # type: ignore[attr-defined]
    audit = _terminal_audits(service)[0]
    assert audit["principal_id"] == OWNER_PRINCIPAL
    assert audit["auth_subject_id"] == f"auth-{OWNER_PRINCIPAL}"
    assert audit["agent_instance_id"] == f"instance-{OWNER_PRINCIPAL}"
    assert audit["role"] == Role.CELL_OWNER.value
    assert audit["cell_id"] == CELL_A
    assert audit["agent_run_id"] == OWNER_RUN


@pytest.mark.asyncio
async def test_denied_mutation_is_attributed_to_the_identity_of_the_selected_token(
    signing_key: object,
    bindings: list[SubjectIdentityBinding],
) -> None:
    verifier, service, server = _stack(signing_key, bindings)

    result = await _call_with_token(
        verifier,
        server,
        _raw_token(signing_key, subject=EVALUATOR_SUBJECT),
        "allocation.commit",
        {"cell_id": CELL_A, "proposal_id": "proposal-1"},
    )

    assert result.is_error is True  # type: ignore[attr-defined]
    assert len(service.audits) == 2
    received = service.audits[0]
    assert received["phase"] == "received"
    assert received["authorization_outcome"] == "pending"
    assert received["execution_outcome"] == "pending"
    audit = _terminal_audits(service)[0]
    assert audit["call_id"] == received["call_id"]
    assert audit["request_id"] == received["request_id"]
    assert audit["arguments_sha256"] == received["arguments_sha256"]
    assert audit["principal_id"] == EVALUATOR_PRINCIPAL
    assert audit["auth_subject_id"] == f"auth-{EVALUATOR_PRINCIPAL}"
    assert audit["agent_instance_id"] == f"instance-{EVALUATOR_PRINCIPAL}"
    assert audit["definition_id"] == "definition-evaluator"
    assert audit["definition_version"] == 1
    assert audit["role"] == Role.EVALUATOR.value
    assert audit["cell_id"] == CELL_A
    assert audit["agent_run_id"] == EVALUATOR_RUN
    assert audit["tool_name"] == "allocation.commit"
    assert audit["authorization_outcome"] == "denied"
    assert audit["authorization_reason_code"] == "capability_not_granted"
    assert audit["execution_outcome"] == "not_executed"


@pytest.mark.asyncio
async def test_cross_cell_attempt_is_denied_and_attributed(
    signing_key: object,
    bindings: list[SubjectIdentityBinding],
) -> None:
    verifier, service, server = _stack(signing_key, bindings)

    result = await _call_with_token(
        verifier,
        server,
        _raw_token(signing_key, subject=OWNER_SUBJECT),
        "cell.get",
        {"cell_id": CELL_B},
    )

    assert result.is_error is True  # type: ignore[attr-defined]
    audit = _terminal_audits(service)[0]
    assert audit["principal_id"] == OWNER_PRINCIPAL
    assert audit["resource_id"] == CELL_B
    assert audit["authorization_outcome"] == "denied"
    assert audit["authorization_reason_code"] == "scope_not_granted"


@pytest.mark.asyncio
async def test_attribution_is_content_minimized_and_never_stores_tokens_or_payloads(
    signing_key: object,
    bindings: list[SubjectIdentityBinding],
) -> None:
    verifier, service, server = _stack(signing_key, bindings)
    raw_token = _raw_token(signing_key, subject=OWNER_SUBJECT)
    secret = "payload-secret-must-not-be-persisted"
    payload = {"cell_id": CELL_A, "secret": secret}

    result = await _call_with_token(
        verifier,
        server,
        raw_token,
        "cell.get",
        payload,
    )

    assert result.is_error is False  # type: ignore[attr-defined]
    serialized_audit = json.dumps(service.audits)
    assert raw_token not in serialized_audit
    assert secret not in serialized_audit
    assert "payload" not in service.audits[0]
    assert (
        service.audits[0]["arguments_sha256"]
        == hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.asyncio
async def test_unbound_subject_fails_before_substrate_dispatch(
    signing_key: object,
    bindings: list[SubjectIdentityBinding],
) -> None:
    verifier, service, server = _stack(signing_key, bindings)

    result = await _call_with_token(
        verifier,
        server,
        _raw_token(signing_key, subject="unbound-subject"),
        "cell.get",
        {"cell_id": CELL_A},
    )

    assert result.is_error is True  # type: ignore[attr-defined]
    assert service.audits == []

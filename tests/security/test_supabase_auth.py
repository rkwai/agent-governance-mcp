"""Contracts for Supabase JWT verification and trusted identity binding."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken

from ai_nature_company.auth import (
    AgentIdentityContext,
    AgentRunExpiredError,
    AgentRunRevokedError,
    AgentRunSuspendedError,
    AuthenticationError,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
    is_verified_principal,
)
from ai_nature_company.mcp_resource import (
    bind_request_mcp_resource_url,
    reset_request_mcp_resource_url,
)
from ai_nature_company.supabase_auth import (
    InMemorySubjectIdentityRepository,
    SubjectIdentityBinding,
    SupabaseIdentityAuthenticator,
    SupabaseJWTSettings,
    SupabaseJWTVerifier,
)

ISSUER = "https://project-ref.supabase.co/auth/v1"
AUDIENCE = "authenticated"
CLIENT_ID = "grok-owner-connector"
SUBJECT = "10000000-0000-4000-8000-000000000001"
SESSION_ID = "40000000-0000-4000-8000-000000000001"
RESOURCE = "https://mcp.example.test/mcp"


class _StaticSigningKeys:
    def __init__(self, public_key: object) -> None:
        self.public_key = public_key
        self.tokens: list[str] = []

    async def signing_key(self, token: str) -> object:
        self.tokens.append(token)
        return self.public_key


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[object, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _claims(**changes: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "client_id": CLIENT_ID,
        "session_id": SESSION_ID,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "scope": "openid profile",
        "role": "founder",  # Untrusted application authority and deliberately ignored.
        "company_id": "attacker-company",
    }
    claims.update(changes)
    return claims


def _jwt(private_key: object, claims: dict[str, Any] | None = None) -> str:
    return jwt.encode(
        claims or _claims(),
        private_key,
        algorithm="RS256",
        headers={"kid": "test-signing-key"},
    )


@pytest.mark.asyncio
async def test_verifier_accepts_a_strict_supabase_access_token(
    rsa_keys: tuple[object, object],
) -> None:
    private_key, public_key = rsa_keys
    token = _jwt(private_key)
    keys = _StaticSigningKeys(public_key)
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE,
            allowed_client_ids=frozenset({CLIENT_ID}),
        ),
        signing_keys=keys,
    )

    access_token = await verifier.verify_token(token)

    assert access_token is not None
    assert access_token.token == token
    assert access_token.subject == SUBJECT
    assert access_token.client_id == CLIENT_ID
    assert access_token.resource == RESOURCE
    assert access_token.scopes == ["openid", "profile"]
    assert access_token.claims == {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": pytest.approx(datetime.now(UTC).timestamp(), abs=2),
        "session_id": SESSION_ID,
    }
    assert keys.tokens == [token]
    assert "founder" not in repr(access_token.claims)
    assert "attacker-company" not in repr(access_token.claims)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_changes",
    [
        {"iss": "https://attacker.example/auth/v1"},
        {"aud": "wrong-audience"},
        {"client_id": "unregistered-connector"},
        {"exp": 1},
        {"iat": "not-a-numeric-date"},
        {"session_id": "not-a-uuid"},
        {"sub": ""},
    ],
)
async def test_verifier_rejects_invalid_identity_or_lifetime_claims_without_leaking_token(
    rsa_keys: tuple[object, object], claim_changes: dict[str, Any]
) -> None:
    private_key, public_key = rsa_keys
    token = _jwt(private_key, _claims(**claim_changes))
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE,
            allowed_client_ids=frozenset({CLIENT_ID}),
        ),
        signing_keys=_StaticSigningKeys(public_key),
    )

    assert await verifier.verify_token(token) is None
    assert token not in repr(verifier)


@pytest.mark.asyncio
async def test_verifier_requires_every_security_claim(
    rsa_keys: tuple[object, object],
) -> None:
    private_key, public_key = rsa_keys
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE,
        ),
        signing_keys=_StaticSigningKeys(public_key),
    )

    for missing in ("iss", "aud", "sub", "client_id", "session_id", "iat", "exp"):
        claims = _claims()
        del claims[missing]
        assert await verifier.verify_token(_jwt(private_key, claims)) is None


@pytest.mark.asyncio
async def test_verifier_rejects_a_token_signed_by_an_untrusted_key(
    rsa_keys: tuple[object, object],
) -> None:
    trusted_private, trusted_public = rsa_keys
    del trusted_private
    attacker_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE,
        ),
        signing_keys=_StaticSigningKeys(trusted_public),
    )

    assert await verifier.verify_token(_jwt(attacker_private)) is None


@pytest.mark.asyncio
async def test_verifier_accepts_azp_as_the_oauth_client_identifier(
    rsa_keys: tuple[object, object],
) -> None:
    private_key, public_key = rsa_keys
    claims = _claims(azp=CLIENT_ID)
    del claims["client_id"]
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE,
            allowed_client_ids=frozenset({CLIENT_ID}),
        ),
        signing_keys=_StaticSigningKeys(public_key),
    )

    access_token = await verifier.verify_token(_jwt(private_key, claims))

    assert access_token is not None
    assert access_token.client_id == CLIENT_ID


@pytest.mark.asyncio
async def test_verifier_requires_a_key_id_before_jwks_selection(
    rsa_keys: tuple[object, object],
) -> None:
    private_key, public_key = rsa_keys
    token = jwt.encode(_claims(), private_key, algorithm="RS256")
    keys = _StaticSigningKeys(public_key)
    verifier = SupabaseJWTVerifier(
        SupabaseJWTSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            resource_server_url=RESOURCE,
        ),
        signing_keys=keys,
    )

    assert await verifier.verify_token(token) is None
    assert keys.tokens == []


def _principal(
    principal_id: str,
    role: Role,
    *,
    agent_run_id: str,
    disabled: bool = False,
    cell_id: str | None = "cell-1",
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
        grants=(ScopedRoleGrant(role, ResourceScope.company("company-1")),),
        disabled=disabled,
        agent_run_id=agent_run_id,
        agent_identity=agent_identity,
        auth_subject_id=f"auth-{principal_id}",
    )


def _binding(
    principal: PrincipalRecord,
    *,
    subject: str = SUBJECT,
    client_id: str = CLIENT_ID,
    issuer: str = ISSUER,
    token_valid_after: datetime | None = None,
    run_expires_at: datetime | None = None,
    run_status: str = "running",
    instance_status: str = "active",
) -> SubjectIdentityBinding:
    identity = principal.agent_identity
    assert identity is not None
    assert principal.agent_run_id is not None
    assert principal.auth_subject_id is not None
    return SubjectIdentityBinding(
        issuer=issuer,
        subject=subject,
        client_id=client_id,
        principal=principal,
        auth_subject_id=principal.auth_subject_id,
        agent_instance_id=identity.agent_instance_id,
        definition_id=identity.definition_id,
        definition_version=identity.definition_version,
        cell_id=identity.cell_id,
        assigned_role=identity.role,
        agent_run_id=principal.agent_run_id,
        token_valid_after=token_valid_after,
        run_expires_at=run_expires_at,
        run_status=run_status,
        instance_status=instance_status,
    )


def _access_token(
    *,
    subject: str = SUBJECT,
    client_id: str = CLIENT_ID,
    issuer: str = ISSUER,
    issued_at: int | None = None,
    resource: str = RESOURCE,
) -> AccessToken:
    return AccessToken(
        token="secret-bearer-token",
        client_id=client_id,
        scopes=["openid"],
        subject=subject,
        resource=resource,
        claims={
            "iss": issuer,
            "aud": AUDIENCE,
            "iat": issued_at or int(datetime.now(UTC).timestamp()),
            "session_id": SESSION_ID,
        },
    )


@pytest.mark.asyncio
async def test_subject_binding_issues_only_server_owned_role_and_run_authority() -> None:
    binding = _binding(
        _principal(
            "20000000-0000-4000-8000-000000000001",
            Role.CELL_OWNER,
            agent_run_id="30000000-0000-4000-8000-000000000001",
        ),
    )
    authenticator = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository([binding]),
        resource_server_url=RESOURCE,
    )
    access_token = _access_token()
    access_token.claims.update(  # type: ignore[union-attr]
        {"role": "founder", "company_id": "other-company", "agent_run_id": "spoofed"}
    )

    authority = await authenticator(access_token)

    assert is_verified_principal(authority)
    assert authority.principal_id == binding.principal.principal_id
    assert authority.roles == {Role.CELL_OWNER}
    assert authority.company_id == "company-1"
    assert authority.agent_run_id == binding.principal.agent_run_id
    assert authority.agent_run_id == binding.agent_run_id
    assert authority.agent_identity == binding.principal.agent_identity
    assert authority.auth_subject_id == binding.auth_subject_id
    assert authority.authentication_method == "supabase-oauth"
    assert authority.authentication_id.startswith("supabase:")
    assert "secret-bearer-token" not in repr(authority)


@pytest.mark.asyncio
async def test_distinct_subjects_resolve_to_distinct_agent_principals() -> None:
    second_subject = "10000000-0000-4000-8000-000000000002"
    bindings = [
        _binding(
            _principal(
                "20000000-0000-4000-8000-000000000001",
                Role.CELL_OWNER,
                agent_run_id="30000000-0000-4000-8000-000000000001",
            ),
            subject=SUBJECT,
        ),
        _binding(
            _principal(
                "20000000-0000-4000-8000-000000000002",
                Role.EVALUATOR,
                agent_run_id="30000000-0000-4000-8000-000000000002",
                cell_id=None,
            ),
            subject=second_subject,
        ),
    ]
    authenticator = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository(bindings),
        resource_server_url=RESOURCE,
    )

    owner = await authenticator(_access_token())
    evaluator = await authenticator(_access_token(subject=second_subject))

    assert owner.principal_id != evaluator.principal_id
    assert owner.authentication_id != evaluator.authentication_id
    assert owner.roles == {Role.CELL_OWNER}
    assert evaluator.roles == {Role.EVALUATOR}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "access_token",
    [
        _access_token(subject="unknown-subject"),
        _access_token(client_id="wrong-client"),
        _access_token(issuer="https://other-project.supabase.co/auth/v1"),
        _access_token(resource="https://other-resource.example.test/mcp"),
        _access_token(resource="https://other-resource.example.test/mcp/operating-specialist-1"),
    ],
)
async def test_identity_resolution_fails_closed_for_an_unbound_identity(
    access_token: AccessToken,
) -> None:
    binding = _binding(
        _principal("principal-1", Role.SPECIALIST, agent_run_id="run-1"),
    )
    authenticator = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository([binding]),
        resource_server_url=RESOURCE,
    )

    with pytest.raises(AuthenticationError) as caught:
        await authenticator(access_token)

    assert str(caught.value) == "authentication failed"
    assert "secret-bearer-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_disabled_binding_and_tokens_issued_before_revocation_are_rejected() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    disabled = _binding(
        _principal("principal-disabled", Role.SPECIALIST, agent_run_id="run-1", disabled=True),
        subject="disabled-subject",
    )
    revoked = _binding(
        _principal("principal-revoked", Role.SPECIALIST, agent_run_id="run-2"),
        token_valid_after=now,
    )
    authenticator = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository([disabled, revoked]),
        resource_server_url=RESOURCE,
    )

    with pytest.raises(AuthenticationError, match="authentication failed"):
        await authenticator(_access_token(subject="disabled-subject"))
    with pytest.raises(AuthenticationError, match="authentication failed"):
        await authenticator(_access_token(issued_at=int((now - timedelta(seconds=1)).timestamp())))

    accepted = await authenticator(_access_token(issued_at=int(now.timestamp())))
    assert accepted.principal_id == "principal-revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_fields", "error_type", "code", "needle"),
    [
        (
            {
                "run_expires_at": datetime(2026, 10, 1, 6, 59, 59, tzinfo=UTC),
                "run_status": "running",
            },
            AgentRunExpiredError,
            "run_expired",
            "expired at 2026-10-01T06:59:59Z",
        ),
        (
            {"run_status": "revoked"},
            AgentRunRevokedError,
            "run_revoked",
            "is revoked",
        ),
        (
            {"instance_status": "suspended"},
            AgentRunSuspendedError,
            "run_suspended",
            "is suspended",
        ),
    ],
)
async def test_inactive_caller_run_returns_a_specific_auth_error(
    status_fields: dict[str, object],
    error_type: type[AuthenticationError],
    code: str,
    needle: str,
) -> None:
    binding = _binding(
        _principal("principal-1", Role.SPECIALIST, agent_run_id="run-owner-1"),
        **status_fields,
    )
    authenticator = SupabaseIdentityAuthenticator(
        InMemorySubjectIdentityRepository([binding]),
        resource_server_url=RESOURCE,
    )

    with pytest.raises(error_type) as caught:
        await authenticator(_access_token())

    assert caught.value.code == code
    assert caught.value.agent_run_id == "run-owner-1"
    assert needle in str(caught.value)
    assert "secret-bearer-token" not in str(caught.value)
    assert "principal-2" not in str(caught.value)


@pytest.mark.asyncio
async def test_authenticator_resolves_identity_against_the_request_path_resource() -> None:
    binding = _binding(
        _principal("principal-1", Role.SPECIALIST, agent_run_id="run-1"),
    )
    seen: list[str] = []

    class _RecordingRepository(InMemorySubjectIdentityRepository):
        async def resolve(self, **kwargs: object):  # type: ignore[override]
            resource = kwargs["resource_server_url"]
            assert isinstance(resource, str)
            seen.append(resource)
            return await super().resolve(**kwargs)

    authenticator = SupabaseIdentityAuthenticator(
        _RecordingRepository([binding]),
        resource_server_url=RESOURCE,
    )
    path_resource = f"{RESOURCE}/operating-specialist-1"
    token = bind_request_mcp_resource_url(path_resource)
    try:
        authority = await authenticator(_access_token())
    finally:
        reset_request_mcp_resource_url(token)

    assert authority.principal_id == "principal-1"
    assert seen == [path_resource]


@pytest.mark.asyncio
async def test_authenticator_accepts_a_same_origin_path_alias_on_the_token() -> None:
    binding = _binding(
        _principal("principal-1", Role.SPECIALIST, agent_run_id="run-1"),
    )
    seen: list[str] = []

    class _RecordingRepository(InMemorySubjectIdentityRepository):
        async def resolve(self, **kwargs: object):  # type: ignore[override]
            resource = kwargs["resource_server_url"]
            assert isinstance(resource, str)
            seen.append(resource)
            return await super().resolve(**kwargs)

    authenticator = SupabaseIdentityAuthenticator(
        _RecordingRepository([binding]),
        resource_server_url=RESOURCE,
    )

    authority = await authenticator(_access_token(resource=f"{RESOURCE}/operating-specialist-1"))

    assert authority.principal_id == "principal-1"
    assert seen == [f"{RESOURCE}/operating-specialist-1"]


def test_identity_repository_rejects_ambiguous_duplicate_bindings() -> None:
    binding = _binding(
        _principal("principal-1", Role.SPECIALIST, agent_run_id="run-1"),
    )

    with pytest.raises(ValueError, match="duplicate subject identity binding"):
        InMemorySubjectIdentityRepository([binding, binding])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"agent_instance_id": "other-instance"}, "agent identity"),
        ({"definition_id": "other-definition"}, "agent identity"),
        ({"definition_version": 2}, "agent identity"),
        ({"cell_id": "cell-2"}, "agent identity"),
        ({"assigned_role": Role.EVALUATOR}, "only granted role"),
        ({"agent_run_id": "other-run"}, "active agent run"),
        ({"auth_subject_id": "other-auth-subject"}, "auth subject"),
    ],
)
def test_subject_binding_must_exactly_match_trusted_agent_and_run_context(
    change: dict[str, Any], message: str
) -> None:
    binding = _binding(
        _principal("principal-1", Role.SPECIALIST, agent_run_id="run-1"),
    )

    with pytest.raises(ValueError, match=message):
        replace(binding, **change)


def test_subject_binding_requires_trusted_agent_identity_context() -> None:
    principal = PrincipalRecord(
        "principal-1",
        grants=(ScopedRoleGrant(Role.SPECIALIST, ResourceScope.company("company-1")),),
        agent_run_id="run-1",
        auth_subject_id="auth-principal-1",
    )

    with pytest.raises(ValueError, match="agent identity"):
        SubjectIdentityBinding(
            issuer=ISSUER,
            subject=SUBJECT,
            client_id=CLIENT_ID,
            principal=principal,
            auth_subject_id="auth-principal-1",
            agent_instance_id="instance-principal-1",
            definition_id="definition-specialist",
            definition_version=1,
            cell_id="cell-1",
            assigned_role=Role.SPECIALIST,
            agent_run_id="run-1",
        )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            {
                "issuer": "not-a-url",
                "audience": AUDIENCE,
                "resource_server_url": RESOURCE,
            },
            "issuer must be an absolute HTTP URL",
        ),
        (
            {"issuer": ISSUER, "audience": "", "resource_server_url": RESOURCE},
            "audience",
        ),
        (
            {
                "issuer": ISSUER,
                "audience": AUDIENCE,
                "resource_server_url": RESOURCE,
                "algorithms": ("HS256",),
            },
            "asymmetric",
        ),
        (
            {
                "issuer": ISSUER,
                "audience": AUDIENCE,
                "resource_server_url": RESOURCE,
                "leeway_seconds": -1,
            },
            "leeway_seconds",
        ),
    ],
)
def test_invalid_jwt_configuration_is_rejected(settings: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SupabaseJWTSettings(**settings)

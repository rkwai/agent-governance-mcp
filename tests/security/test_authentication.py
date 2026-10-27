"""Authentication-boundary security contracts."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from ai_nature_company.auth import (
    AGENT_RUN_EXPIRY_WARNING_WINDOW,
    AgentIdentityContext,
    AgentRunExpiredError,
    AuthenticationError,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    ResourceType,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
    inactive_agent_run_error,
    is_verified_principal,
)


def _record(
    principal_id: str = "agent-specialist-1",
    *,
    company_id: str = "company-1",
    role: Role = Role.SPECIALIST,
) -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=principal_id,
        grants=(ScopedRoleGrant(role, ResourceScope.company(company_id)),),
    )


def test_local_authenticator_issues_an_immutable_verified_principal() -> None:
    authenticator = LocalTestAuthenticator({"opaque-test-token": _record()})

    principal = authenticator.authenticate("opaque-test-token")

    assert is_verified_principal(principal)
    assert principal.principal_id == "agent-specialist-1"
    assert principal.authentication_method == "local-test"
    assert principal.authentication_id == "local-test:agent-specialist-1"
    assert principal.auth_subject_id is None
    assert principal.roles == frozenset({Role.SPECIALIST})
    assert "opaque-test-token" not in repr(principal)
    assert "opaque-test-token" not in repr(authenticator)
    with pytest.raises(FrozenInstanceError):
        principal.principal_id = "founder"  # type: ignore[misc]


def test_agent_run_id_is_issued_only_from_trusted_configuration() -> None:
    agent_identity = AgentIdentityContext(
        agent_instance_id="cell-a-specialist-1",
        definition_id="specialist-v1",
        definition_version=1,
        role=Role.SPECIALIST,
        cell_id="cell-a",
    )
    record = PrincipalRecord(
        principal_id="agent-specialist-1",
        grants=(ScopedRoleGrant(Role.SPECIALIST, ResourceScope.company("company-1")),),
        agent_run_id="run-server-1",
        agent_identity=agent_identity,
        auth_subject_id="auth-subject-specialist-1",
    )
    authenticator = LocalTestAuthenticator({"opaque-test-token": record})

    principal = authenticator.authenticate("opaque-test-token")

    assert principal.agent_run_id == "run-server-1"
    assert principal.auth_subject_id == "auth-subject-specialist-1"
    assert principal.agent_identity == agent_identity
    assert principal.agent_identity.agent_instance_id == "cell-a-specialist-1"
    assert principal.agent_identity.definition_id == "specialist-v1"
    assert principal.agent_identity.definition_version == 1
    assert principal.agent_identity.role is Role.SPECIALIST
    assert principal.agent_identity.cell_id == "cell-a"
    assert "opaque-test-token" not in repr(principal)
    assert "verification_marker" not in repr(principal)
    assert not hasattr(principal, "credential")
    with pytest.raises(FrozenInstanceError):
        principal.agent_run_id = "run-payload-spoof"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        principal.auth_subject_id = "auth-subject-spoof"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        principal.agent_identity.cell_id = "cell-b"  # type: ignore[misc]


def test_agent_identity_must_match_a_server_owned_role_grant() -> None:
    identity = AgentIdentityContext(
        agent_instance_id="cell-a-evaluator-1",
        definition_id="evaluator-v1",
        definition_version=1,
        role=Role.EVALUATOR,
        cell_id="cell-a",
    )

    with pytest.raises(ValueError, match="agent identity role"):
        PrincipalRecord(
            principal_id="agent-evaluator-1",
            grants=(ScopedRoleGrant(Role.SPECIALIST, ResourceScope.company("company-1")),),
            agent_identity=identity,
        )

    with pytest.raises(ValueError, match="agent identity role"):
        PrincipalRecord(
            principal_id="agent-evaluator-1",
            grants=(
                ScopedRoleGrant(Role.EVALUATOR, ResourceScope.company("company-1")),
                ScopedRoleGrant(Role.OBSERVER, ResourceScope.company("company-1")),
            ),
            agent_identity=identity,
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AgentIdentityContext("", "definition-1", 1, Role.SPECIALIST),
        lambda: AgentIdentityContext("agent-1", "", 1, Role.SPECIALIST),
        lambda: AgentIdentityContext("agent-1", "definition-1", 0, Role.SPECIALIST),
        lambda: AgentIdentityContext("agent-1", "definition-1", True, Role.SPECIALIST),
        lambda: AgentIdentityContext("agent-1", "definition-1", 1, Role.SPECIALIST, " cell-a"),
    ],
)
def test_agent_identity_rejects_noncanonical_fields(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_agent_run_id_is_optional_and_canonical_when_present() -> None:
    assert _record().agent_run_id is None
    assert LocalTestAuthenticator({"known": _record()}).authenticate("known").agent_run_id is None

    with pytest.raises(ValueError, match="agent_run_id"):
        PrincipalRecord("agent-1", grants=(), agent_run_id=" run-1")
    with pytest.raises(ValueError, match="auth_subject_id"):
        PrincipalRecord("agent-1", grants=(), auth_subject_id=" auth-subject-1")


def test_verified_principal_cannot_be_constructed_from_request_fields() -> None:
    with pytest.raises(TypeError, match="authentication boundary"):
        VerifiedPrincipal(  # type: ignore[call-arg]
            principal_id="attacker",
            grants=(ScopedRoleGrant(Role.FOUNDER, ResourceScope.company("company-1")),),
            authentication_method="caller",
            authentication_id="caller:attacker",
            agent_run_id="run-payload-spoof",
        )


@pytest.mark.parametrize("credential", ["unknown", "", None, 7])
def test_authentication_failure_is_generic_and_fail_closed(credential: object) -> None:
    authenticator = LocalTestAuthenticator({"known": _record()})

    with pytest.raises(AuthenticationError) as caught:
        authenticator.authenticate(credential)  # type: ignore[arg-type]

    assert caught.value.code == "unauthenticated"
    assert str(caught.value) == "authentication failed"
    assert repr(credential) not in str(caught.value)


def test_disabled_principal_cannot_authenticate() -> None:
    authenticator = LocalTestAuthenticator(
        {"known": PrincipalRecord("agent-1", grants=(), disabled=True)}
    )

    with pytest.raises(AuthenticationError, match="authentication failed"):
        authenticator.authenticate("known")


def test_authenticator_copies_its_trusted_configuration() -> None:
    records = {"known": _record()}
    authenticator = LocalTestAuthenticator(records)
    records["added-later"] = _record("attacker", role=Role.FOUNDER)

    with pytest.raises(AuthenticationError):
        authenticator.authenticate("added-later")


def test_company_and_resource_scopes_are_exact_and_company_bounded() -> None:
    company_grant = ResourceScope.company("company-1")
    cell = ResourceScope.resource("company-1", ResourceType.CELL, "cell-1")
    other_cell = ResourceScope.resource("company-1", ResourceType.CELL, "cell-2")
    same_id_other_company = ResourceScope.resource("company-2", ResourceType.CELL, "cell-1")

    assert company_grant.contains(cell)
    assert cell.contains(cell)
    assert not cell.contains(other_cell)
    assert not company_grant.contains(same_id_other_company)
    assert not cell.contains(same_id_other_company)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: ResourceScope.company(""), "company_id"),
        (lambda: ResourceScope.company(" company-1"), "company_id"),
        (
            lambda: ResourceScope(company_id="company-1", resource_type=ResourceType.CELL),
            "resource_type and resource_id",
        ),
        (
            lambda: ResourceScope(company_id="company-1", resource_id="cell-1"),
            "resource_type and resource_id",
        ),
        (
            lambda: ResourceScope.resource("company-1", ResourceType.COMPANY, "company-1"),
            "canonical company scope",
        ),
    ],
)
def test_invalid_scope_definitions_are_rejected(factory: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        factory()  # type: ignore[operator]


def test_principal_record_rejects_ambiguous_or_untrusted_grants() -> None:
    grant = ScopedRoleGrant(Role.SPECIALIST, ResourceScope.company("company-1"))

    with pytest.raises(ValueError, match="duplicate grant"):
        PrincipalRecord("agent-1", grants=(grant, grant))
    with pytest.raises(TypeError, match="ScopedRoleGrant"):
        PrincipalRecord("agent-1", grants=({"role": "founder"},))  # type: ignore[arg-type]


def test_inactive_agent_run_error_names_only_the_caller_run() -> None:
    expires_at = datetime(2026, 10, 1, 6, 59, 59, tzinfo=UTC)
    error = inactive_agent_run_error(
        agent_run_id="run-owner-1",
        run_status="running",
        instance_status="active",
        expires_at=expires_at,
        now=expires_at + timedelta(seconds=1),
    )

    assert isinstance(error, AgentRunExpiredError)
    assert error.code == "run_expired"
    assert error.details == {
        "agent_run_id": "run-owner-1",
        "expires_at": "2026-10-01T06:59:59Z",
    }
    assert "run-other" not in str(error)


def test_verified_principal_exposes_caller_run_expiry_window() -> None:
    expires_at = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)
    record = PrincipalRecord(
        "agent-1",
        grants=(ScopedRoleGrant(Role.SPECIALIST, ResourceScope.company("company-1")),),
        agent_run_id="run-1",
        agent_run_expires_at=expires_at,
    )
    principal = LocalTestAuthenticator({"known": record}).authenticate("known")
    soon = principal.caller_agent_run_view(now=expires_at - timedelta(hours=24))
    later = principal.caller_agent_run_view(
        now=expires_at - AGENT_RUN_EXPIRY_WARNING_WINDOW - timedelta(hours=1)
    )

    assert soon == {
        "agent_run_id": "run-1",
        "expires_at": "2026-10-04T00:00:00Z",
        "expires_soon": True,
    }
    assert later["expires_soon"] is False

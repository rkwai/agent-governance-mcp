"""Deterministic authorization and separation-of-duties contracts."""

from types import MappingProxyType

import pytest

from ai_nature_company.auth import (
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    ResourceType,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
)
from ai_nature_company.errors import AuthorizationError, SeparationOfDutiesError
from ai_nature_company.policy import (
    FOUNDER_ONLY_CAPABILITIES,
    ROLE_CAPABILITIES,
    AuthorizationPolicy,
    CallerAssertions,
    Capability,
)

COMMON_DISCOVERY = frozenset(
    {
        Capability.COMPANY_DESCRIBE,
        Capability.CONSTITUTION_GET,
        Capability.PROTOCOL_DESCRIBE,
        Capability.AGENT_GET,
        Capability.CELL_GET,
        Capability.CELL_LIST,
    }
)
WORK_EXECUTION = frozenset(
    {
        Capability.WORK_CLAIM,
        Capability.WORK_HEARTBEAT,
        Capability.WORK_COMPLETE,
        Capability.WORK_FAIL,
        Capability.WORK_RELEASE,
    }
)
EXPECTED_CAPABILITIES = {
    Role.FOUNDER: COMMON_DISCOVERY
    | {
        Capability.AGENT_REGISTER,
        Capability.AGENT_LIST,
        Capability.WORK_CREATE_ADMIN,
        Capability.CELL_CREATE,
        Capability.CELL_TRANSITION_PAUSE,
        Capability.CELL_TRANSITION_IRREVERSIBLE,
        Capability.MAINTENANCE_RUN,
        Capability.GATE_EVALUATE,
        Capability.ALLOCATION_COMMIT_IRREVERSIBLE,
        Capability.LEARNING_PROMOTE,
        Capability.AUDIT_LIST,
        Capability.DECISION_EXPLAIN,
        Capability.MANAGE_AUTHORIZATION,
        Capability.AMEND_CONSTITUTION,
        Capability.APPROVE_IRREVERSIBLE_ACTION,
    },
    Role.CELL_OWNER: COMMON_DISCOVERY
    | WORK_EXECUTION
    | {
        Capability.CELL_CREATE,
        Capability.WORK_CREATE,
        Capability.WORK_CANCEL,
        Capability.CELL_TRANSITION_OPERATIONAL,
        Capability.CELL_TRANSITION_PAUSE,
        Capability.CELL_TRANSITION_KILL_OWNER_SIGNED,
        Capability.CELL_AMEND,
        Capability.MAINTENANCE_RUN,
        Capability.OFFER_REGISTER,
        Capability.OFFER_MUTATE,
        Capability.EXPERIMENT_REGISTER,
        Capability.EXPERIMENT_START,
        Capability.EXPERIMENT_CLOSE,
        Capability.EXPERIMENT_AMEND,
        Capability.EXPERIMENT_RECORD_SEND,
        Capability.GATE_EVALUATE,
        Capability.ALLOCATION_PROPOSE,
        Capability.ALLOCATION_COMMIT_BOUNDED,
        Capability.AUDIT_LIST,
        Capability.DECISION_EXPLAIN,
    },
    Role.SPECIALIST: COMMON_DISCOVERY
    | WORK_EXECUTION
    | {
        Capability.EVIDENCE_APPEND,
        Capability.ECONOMICS_APPEND_ATTESTED,
        Capability.LEARNING_PROPOSE,
    },
    Role.EVALUATOR: COMMON_DISCOVERY
    | WORK_EXECUTION
    | {
        Capability.EVALUATION_SUBMIT,
        Capability.GATE_EVALUATE,
        Capability.DECISION_EXPLAIN,
    },
    Role.ALLOCATOR: COMMON_DISCOVERY
    | WORK_EXECUTION
    | {
        Capability.GATE_EVALUATE,
        Capability.ALLOCATION_PROPOSE,
        Capability.ALLOCATION_COMMIT_BOUNDED,
        Capability.DECISION_EXPLAIN,
    },
    Role.IMMUNE: COMMON_DISCOVERY
    | {
        Capability.GATE_EVALUATE,
        Capability.CELL_TRANSITION_PAUSE,
        Capability.AUDIT_LIST,
        Capability.DECISION_EXPLAIN,
    },
    Role.OBSERVER: COMMON_DISCOVERY
    | {
        Capability.AGENT_LIST,
        Capability.AUDIT_LIST,
        Capability.DECISION_EXPLAIN,
    },
}


def _principal(
    role: Role,
    *,
    principal_id: str | None = None,
    scope: ResourceScope | None = None,
    additional_roles: tuple[Role, ...] = (),
    agent_run_id: str | None = None,
) -> VerifiedPrincipal:
    principal_id = principal_id or f"{role.value}-1"
    scope = scope or ResourceScope.company("company-1")
    grants = (
        ScopedRoleGrant(role, scope),
        *(ScopedRoleGrant(extra_role, scope) for extra_role in additional_roles),
    )
    record = PrincipalRecord(principal_id, grants=grants, agent_run_id=agent_run_id)
    return LocalTestAuthenticator({"credential": record}).authenticate("credential")


def test_owner_is_a_non_expanding_alias_for_cell_owner() -> None:
    assert Role.OWNER is Role.CELL_OWNER


def test_role_capability_matrix_is_exact_and_immutable() -> None:
    assert isinstance(ROLE_CAPABILITIES, MappingProxyType)
    assert ROLE_CAPABILITIES == EXPECTED_CAPABILITIES
    assert set(ROLE_CAPABILITIES) == set(Role)
    with pytest.raises(TypeError):
        ROLE_CAPABILITIES[Role.CELL_OWNER] = frozenset()  # type: ignore[index]


@pytest.mark.parametrize("role", list(Role))
def test_every_role_allows_exactly_its_mapped_capabilities(role: Role) -> None:
    policy = AuthorizationPolicy()
    principal = _principal(role)
    resource = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")

    actual = {
        capability
        for capability in Capability
        if policy.is_allowed(principal, capability, resource)
    }

    assert actual == EXPECTED_CAPABILITIES[role]


def test_owner_signed_kill_is_cell_owner_only_and_not_founder_only() -> None:
    holders = {
        role
        for role, capabilities in ROLE_CAPABILITIES.items()
        if Capability.CELL_TRANSITION_KILL_OWNER_SIGNED in capabilities
    }
    assert holders == {Role.CELL_OWNER}
    assert Capability.CELL_TRANSITION_KILL_OWNER_SIGNED not in FOUNDER_ONLY_CAPABILITIES
    assert Capability.CELL_TRANSITION_IRREVERSIBLE in FOUNDER_ONLY_CAPABILITIES


def test_founder_is_a_gatekeeper_not_a_superuser() -> None:
    policy = AuthorizationPolicy()
    founder = _principal(Role.FOUNDER)
    work = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")

    for forbidden in (
        Capability.WORK_CLAIM,
        Capability.EVIDENCE_APPEND,
        Capability.ECONOMICS_APPEND_ATTESTED,
        Capability.EVALUATION_SUBMIT,
        Capability.ALLOCATION_PROPOSE,
        Capability.ALLOCATION_COMMIT_BOUNDED,
    ):
        assert not policy.is_allowed(founder, forbidden, work)


def test_unverified_duck_typed_principal_is_denied() -> None:
    class CallerPrincipal:
        principal_id = "attacker"
        grants = (ScopedRoleGrant(Role.FOUNDER, ResourceScope.company("company-1")),)

    decision = AuthorizationPolicy().decide(
        CallerPrincipal(),
        Capability.AMEND_CONSTITUTION,
        ResourceScope.company("company-1"),
    )

    assert not decision.allowed
    assert decision.reason == "unverified_principal"


@pytest.mark.parametrize("capability", ["become_founder", "", None, object()])
def test_unknown_or_malformed_capabilities_are_denied(capability: object) -> None:
    decision = AuthorizationPolicy().decide(
        _principal(Role.FOUNDER),
        capability,  # type: ignore[arg-type]
        ResourceScope.company("company-1"),
    )

    assert not decision.allowed
    assert decision.reason == "unknown_capability"


def test_missing_or_malformed_resource_context_is_denied() -> None:
    policy = AuthorizationPolicy()
    principal = _principal(Role.FOUNDER)

    for resource in (None, {"company_id": "company-1"}, "company-1"):
        decision = policy.decide(
            principal,
            Capability.COMPANY_DESCRIBE,
            resource,  # type: ignore[arg-type]
        )
        assert not decision.allowed
        assert decision.reason == "invalid_resource"


def test_company_grant_never_crosses_company_boundary() -> None:
    policy = AuthorizationPolicy()
    principal = _principal(Role.FOUNDER)

    assert policy.is_allowed(
        principal,
        Capability.AMEND_CONSTITUTION,
        ResourceScope.company("company-1"),
    )
    assert not policy.is_allowed(
        principal,
        Capability.AMEND_CONSTITUTION,
        ResourceScope.company("company-2"),
    )


def test_resource_grant_matches_only_the_exact_resource() -> None:
    policy = AuthorizationPolicy()
    cell_1 = ResourceScope.resource("company-1", ResourceType.CELL, "cell-1")
    principal = _principal(Role.CELL_OWNER, scope=cell_1)

    assert policy.is_allowed(principal, Capability.OFFER_REGISTER, cell_1)
    assert not policy.is_allowed(
        principal,
        Capability.OFFER_REGISTER,
        ResourceScope.resource("company-1", ResourceType.CELL, "cell-2"),
    )
    assert not policy.is_allowed(
        principal,
        Capability.OFFER_REGISTER,
        ResourceScope.company("company-1"),
    )


@pytest.mark.parametrize(
    "assertions",
    [
        CallerAssertions(actor_id="founder-1"),
        CallerAssertions(company_id="company-2"),
        CallerAssertions(role=Role.FOUNDER),
        CallerAssertions(agent_run_id="run-payload-spoof"),
        CallerAssertions(actor_id="specialist-1", company_id="company-1", role=Role.FOUNDER),
    ],
)
def test_caller_assertions_cannot_elevate_authority(
    assertions: CallerAssertions,
) -> None:
    policy = AuthorizationPolicy()
    principal = _principal(Role.SPECIALIST)
    resource = ResourceScope.company("company-1")

    decision = policy.decide(
        principal,
        Capability.AMEND_CONSTITUTION,
        resource,
        assertions=assertions,
    )

    assert not decision.allowed


def test_matching_caller_assertions_do_not_replace_verified_authority() -> None:
    policy = AuthorizationPolicy()
    principal = _principal(Role.SPECIALIST, agent_run_id="run-server-1")
    resource = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")
    assertions = CallerAssertions(
        actor_id="specialist-1",
        company_id="company-1",
        role=Role.SPECIALIST,
        agent_run_id="run-server-1",
    )

    assert policy.is_allowed(
        principal,
        Capability.WORK_CLAIM,
        resource,
        assertions=assertions,
    )
    assert not policy.is_allowed(
        principal,
        Capability.ALLOCATION_PROPOSE,
        resource,
        assertions=assertions,
    )


def test_caller_cannot_spoof_verified_agent_run_identity() -> None:
    policy = AuthorizationPolicy()
    principal = _principal(Role.SPECIALIST, agent_run_id="run-server-1")
    resource = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")

    decision = policy.decide(
        principal,
        Capability.WORK_CLAIM,
        resource,
        assertions=CallerAssertions(agent_run_id="run-payload-spoof"),
    )

    assert not decision.allowed
    assert decision.reason == "caller_agent_run_mismatch"


@pytest.mark.parametrize(
    "role",
    [
        Role.CELL_OWNER,
        Role.SPECIALIST,
        Role.EVALUATOR,
        Role.ALLOCATOR,
        Role.IMMUNE,
        Role.OBSERVER,
    ],
)
def test_only_founder_can_pass_irreversible_action_gate(role: Role) -> None:
    policy = AuthorizationPolicy()
    resource = ResourceScope.resource("company-1", ResourceType.EXTERNAL_ACTION, "action-1")

    with pytest.raises(AuthorizationError):
        policy.require_founder_action(
            _principal(role),
            resource,
            action="publish_and_charge_customer",
        )

    decision = policy.require_founder_action(
        _principal(Role.FOUNDER),
        resource,
        action="publish_and_charge_customer",
    )
    assert decision.allowed
    assert decision.matched_role is Role.FOUNDER


def test_constitution_and_authorization_changes_are_founder_only() -> None:
    policy = AuthorizationPolicy()
    company = ResourceScope.company("company-1")

    for role in (
        Role.CELL_OWNER,
        Role.SPECIALIST,
        Role.EVALUATOR,
        Role.ALLOCATOR,
        Role.IMMUNE,
        Role.OBSERVER,
    ):
        principal = _principal(role)
        assert not policy.is_allowed(principal, Capability.AMEND_CONSTITUTION, company)
        assert not policy.is_allowed(principal, Capability.MANAGE_AUTHORIZATION, company)


def test_narrow_founder_grant_cannot_combine_with_company_non_founder_grant() -> None:
    policy = AuthorizationPolicy()
    company = ResourceScope.company("company-1")
    record = PrincipalRecord(
        "mixed-scope-principal",
        grants=(
            ScopedRoleGrant(Role.EVALUATOR, company),
            ScopedRoleGrant(
                Role.FOUNDER,
                ResourceScope.resource("company-1", ResourceType.CELL, "cell-1"),
            ),
        ),
    )
    principal = LocalTestAuthenticator({"credential": record}).authenticate("credential")

    assert policy.is_allowed(principal, Capability.GATE_EVALUATE, company)
    with pytest.raises(AuthorizationError):
        policy.require_founder_action(
            principal,
            company,
            action="allocation.kill",
        )


def test_evaluator_must_be_independent_of_executor() -> None:
    policy = AuthorizationPolicy()
    work = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")
    evaluator = _principal(Role.EVALUATOR, principal_id="evaluator-1")

    decision = policy.require_independent_evaluator(
        evaluator,
        work,
        executed_by="specialist-1",
    )
    assert decision.allowed

    with pytest.raises(SeparationOfDutiesError):
        policy.require_independent_evaluator(
            evaluator,
            work,
            executed_by="evaluator-1",
        )


def test_switching_or_combining_roles_cannot_bypass_separation() -> None:
    policy = AuthorizationPolicy()
    work = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")
    principal = _principal(
        Role.SPECIALIST,
        principal_id="agent-with-two-roles",
        additional_roles=(Role.EVALUATOR,),
    )

    with pytest.raises(SeparationOfDutiesError):
        policy.require_independent_evaluator(
            principal,
            work,
            executed_by="agent-with-two-roles",
        )


def test_founder_cannot_submit_an_evaluation() -> None:
    policy = AuthorizationPolicy()
    work = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")

    with pytest.raises(AuthorizationError):
        policy.require_independent_evaluator(
            _principal(Role.FOUNDER, principal_id="founder-1"),
            work,
            executed_by="specialist-1",
        )


def test_non_evaluator_cannot_pass_independence_check() -> None:
    policy = AuthorizationPolicy()
    work = ResourceScope.resource("company-1", ResourceType.WORK_ITEM, "work-1")

    with pytest.raises(AuthorizationError):
        policy.require_independent_evaluator(
            _principal(Role.SPECIALIST, principal_id="specialist-2"),
            work,
            executed_by="specialist-1",
        )


def test_require_raises_a_stable_safe_authorization_error() -> None:
    policy = AuthorizationPolicy()

    with pytest.raises(AuthorizationError) as caught:
        policy.require(
            _principal(Role.SPECIALIST),
            Capability.ALLOCATION_PROPOSE,
            ResourceScope.company("company-1"),
        )

    assert caught.value.code == "forbidden"
    assert str(caught.value) == "authorization denied"


def test_learning_propose_is_specialist_and_promote_is_founder_only() -> None:
    assigned = frozenset().union(*ROLE_CAPABILITIES.values())

    assert Capability.LEARNING_PROPOSE in assigned
    assert Capability.LEARNING_PROPOSE in ROLE_CAPABILITIES[Role.SPECIALIST]
    assert Capability.LEARNING_PROPOSE not in ROLE_CAPABILITIES[Role.FOUNDER]
    assert Capability.LEARNING_PROMOTE in ROLE_CAPABILITIES[Role.FOUNDER]
    assert Capability.LEARNING_PROMOTE not in ROLE_CAPABILITIES[Role.SPECIALIST]
    assert Capability.LEARNING_PROMOTE in FOUNDER_ONLY_CAPABILITIES

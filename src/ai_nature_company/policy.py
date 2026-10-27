"""Deterministic, default-deny role and capability policy.

Authority comes exclusively from a :class:`VerifiedPrincipal` issued at the
authentication boundary.  Any actor, company, or role repeated in a tool payload is
only an untrusted assertion and can never add a grant.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from .auth import (
    ResourceScope,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
    is_verified_principal,
)
from .errors import AuthorizationError, SeparationOfDutiesError


class Capability(StrEnum):
    """Versioned Phase 1 operations, including risk-specific action variants."""

    COMPANY_DESCRIBE = "company.describe"
    CONSTITUTION_GET = "constitution.get"
    PROTOCOL_DESCRIBE = "protocol.describe"
    AGENT_REGISTER = "agent.register"
    AGENT_GET = "agent.get"
    AGENT_LIST = "agent.list"
    WORK_CREATE = "work.create"
    WORK_CREATE_ADMIN = "work.create.admin"
    WORK_CLAIM = "work.claim"
    WORK_HEARTBEAT = "work.heartbeat"
    WORK_COMPLETE = "work.complete"
    WORK_FAIL = "work.fail"
    WORK_RELEASE = "work.release"
    WORK_CANCEL = "work.cancel"
    CELL_CREATE = "cell.create"
    CELL_GET = "cell.get"
    CELL_LIST = "cell.list"
    CELL_TRANSITION_OPERATIONAL = "cell.transition.operational"
    CELL_TRANSITION_PAUSE = "cell.transition.pause"
    CELL_TRANSITION_IRREVERSIBLE = "cell.transition.irreversible"
    # Owner-signed kill: the cell owner carries out a founder decision that is
    # recorded as founder_decision_ref.  It is never a founder-only grant.
    CELL_TRANSITION_KILL_OWNER_SIGNED = "cell.transition.kill_owner_signed"
    CELL_AMEND = "cell.amend"
    # On-demand Maintain/Continue scan; optionally ends runs on killed cells.
    MAINTENANCE_RUN = "maintenance.run"
    OFFER_REGISTER = "offer.register"
    OFFER_MUTATE = "offer.mutate"
    EXPERIMENT_REGISTER = "experiment.register"
    EXPERIMENT_START = "experiment.start"
    EXPERIMENT_CLOSE = "experiment.close"
    EXPERIMENT_AMEND = "experiment.amend"
    EXPERIMENT_RECORD_SEND = "experiment.record_send"
    EVIDENCE_APPEND = "evidence.append"
    ECONOMICS_APPEND_ATTESTED = "economics.append.attested"
    EVALUATION_SUBMIT = "evaluation.submit"
    GATE_EVALUATE = "gate.evaluate"
    ALLOCATION_PROPOSE = "allocation.propose"
    ALLOCATION_COMMIT_BOUNDED = "allocation.commit.bounded"
    ALLOCATION_COMMIT_IRREVERSIBLE = "allocation.commit.irreversible"
    LEARNING_PROPOSE = "learning.propose"
    LEARNING_PROMOTE = "learning.promote"
    AUDIT_LIST = "audit.list"
    DECISION_EXPLAIN = "decision.explain"
    MANAGE_AUTHORIZATION = "authorization.manage"
    AMEND_CONSTITUTION = "constitution.amend"
    APPROVE_IRREVERSIBLE_ACTION = "founder.approve_irreversible_action"


_COMMON_DISCOVERY = frozenset(
    {
        Capability.COMPANY_DESCRIBE,
        Capability.CONSTITUTION_GET,
        Capability.PROTOCOL_DESCRIBE,
        Capability.AGENT_GET,
        Capability.CELL_GET,
        Capability.CELL_LIST,
    }
)
_WORK_EXECUTION = frozenset(
    {
        Capability.WORK_CLAIM,
        Capability.WORK_HEARTBEAT,
        Capability.WORK_COMPLETE,
        Capability.WORK_FAIL,
        Capability.WORK_RELEASE,
    }
)


# This is deliberately explicit rather than calculated from "all capabilities".
# Adding a capability must not silently turn the founder (or any role) into a superuser.
ROLE_CAPABILITIES: Mapping[Role, frozenset[Capability]] = MappingProxyType(
    {
        Role.FOUNDER: _COMMON_DISCOVERY
        | frozenset(
            {
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
            }
        ),
        Role.CELL_OWNER: _COMMON_DISCOVERY
        | _WORK_EXECUTION
        | frozenset(
            {
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
            }
        ),
        Role.SPECIALIST: _COMMON_DISCOVERY
        | _WORK_EXECUTION
        | frozenset(
            {
                Capability.EVIDENCE_APPEND,
                Capability.ECONOMICS_APPEND_ATTESTED,
                Capability.LEARNING_PROPOSE,
            }
        ),
        Role.EVALUATOR: _COMMON_DISCOVERY
        | _WORK_EXECUTION
        | frozenset(
            {
                Capability.EVALUATION_SUBMIT,
                Capability.GATE_EVALUATE,
                Capability.DECISION_EXPLAIN,
            }
        ),
        Role.ALLOCATOR: _COMMON_DISCOVERY
        | _WORK_EXECUTION
        | frozenset(
            {
                Capability.GATE_EVALUATE,
                Capability.ALLOCATION_PROPOSE,
                Capability.ALLOCATION_COMMIT_BOUNDED,
                Capability.DECISION_EXPLAIN,
            }
        ),
        Role.IMMUNE: _COMMON_DISCOVERY
        | frozenset(
            {
                Capability.GATE_EVALUATE,
                Capability.CELL_TRANSITION_PAUSE,
                Capability.AUDIT_LIST,
                Capability.DECISION_EXPLAIN,
            }
        ),
        Role.OBSERVER: _COMMON_DISCOVERY
        | frozenset(
            {
                Capability.AGENT_LIST,
                Capability.AUDIT_LIST,
                Capability.DECISION_EXPLAIN,
            }
        ),
    }
)

FOUNDER_ONLY_CAPABILITIES = frozenset(
    {
        Capability.CELL_TRANSITION_IRREVERSIBLE,
        Capability.ALLOCATION_COMMIT_IRREVERSIBLE,
        Capability.LEARNING_PROMOTE,
        Capability.MANAGE_AUTHORIZATION,
        Capability.AMEND_CONSTITUTION,
        Capability.APPROVE_IRREVERSIBLE_ACTION,
    }
)


@dataclass(frozen=True, slots=True)
class CallerAssertions:
    """Optional untrusted compatibility fields that may only narrow a decision."""

    actor_id: str | None = None
    company_id: str | None = None
    role: Role | str | None = None
    agent_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Explainable result of one deterministic policy evaluation."""

    allowed: bool
    reason: str
    principal_id: str | None
    capability: Capability | None
    resource: ResourceScope | None
    matched_role: Role | None = None
    matched_scope: ResourceScope | None = None


def _denied(
    reason: str,
    *,
    principal_id: str | None = None,
    capability: Capability | None = None,
    resource: ResourceScope | None = None,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False,
        reason=reason,
        principal_id=principal_id,
        capability=capability,
        resource=resource,
    )


class AuthorizationPolicy:
    """Pure, immutable and fail-closed Phase 1 authorization policy."""

    def decide(
        self,
        principal: object,
        capability: Capability | str,
        resource: ResourceScope,
        *,
        assertions: CallerAssertions | None = None,
    ) -> AuthorizationDecision:
        """Evaluate verified grants against the exact static capability matrix."""

        try:
            requested = Capability(capability)
        except (TypeError, ValueError):
            return _denied("unknown_capability")
        if not is_verified_principal(principal):
            return _denied("unverified_principal", capability=requested)
        verified: VerifiedPrincipal = principal
        if not isinstance(resource, ResourceScope):
            return _denied(
                "invalid_resource",
                principal_id=verified.principal_id,
                capability=requested,
            )
        applicable_grants = verified.grants_for(resource)
        assertion_error = self._assertion_error(
            verified,
            resource,
            applicable_grants,
            assertions,
        )
        if assertion_error is not None:
            return _denied(
                assertion_error,
                principal_id=verified.principal_id,
                capability=requested,
                resource=resource,
            )
        if not applicable_grants:
            return _denied(
                "scope_not_granted",
                principal_id=verified.principal_id,
                capability=requested,
                resource=resource,
            )
        ordered_grants = sorted(
            applicable_grants,
            key=lambda grant: (
                grant.role.value,
                grant.scope.company_id,
                grant.scope.resource_type.value if grant.scope.resource_type else "",
                grant.scope.resource_id or "",
            ),
        )
        for grant in ordered_grants:
            if requested not in ROLE_CAPABILITIES.get(grant.role, frozenset()):
                continue
            if requested in FOUNDER_ONLY_CAPABILITIES and grant.role is not Role.FOUNDER:
                continue
            return AuthorizationDecision(
                allowed=True,
                reason="granted",
                principal_id=verified.principal_id,
                capability=requested,
                resource=resource,
                matched_role=grant.role,
                matched_scope=grant.scope,
            )
        return _denied(
            "capability_not_granted",
            principal_id=verified.principal_id,
            capability=requested,
            resource=resource,
        )

    @staticmethod
    def _assertion_error(
        principal: VerifiedPrincipal,
        resource: ResourceScope,
        applicable_grants: tuple[ScopedRoleGrant, ...],
        assertions: CallerAssertions | None,
    ) -> str | None:
        if assertions is None:
            return None
        if not isinstance(assertions, CallerAssertions):
            return "invalid_caller_assertions"
        if assertions.actor_id is not None and assertions.actor_id != principal.principal_id:
            return "caller_actor_mismatch"
        if assertions.company_id is not None and assertions.company_id != resource.company_id:
            return "caller_company_mismatch"
        if (
            assertions.agent_run_id is not None
            and assertions.agent_run_id != principal.agent_run_id
        ):
            return "caller_agent_run_mismatch"
        if assertions.role is not None:
            try:
                asserted_role = Role(assertions.role)
            except (TypeError, ValueError):
                return "caller_role_mismatch"
            if asserted_role not in {grant.role for grant in applicable_grants}:
                return "caller_role_mismatch"
        return None

    def is_allowed(
        self,
        principal: object,
        capability: Capability | str,
        resource: ResourceScope,
        *,
        assertions: CallerAssertions | None = None,
    ) -> bool:
        return self.decide(
            principal,
            capability,
            resource,
            assertions=assertions,
        ).allowed

    def require(
        self,
        principal: object,
        capability: Capability | str,
        resource: ResourceScope,
        *,
        assertions: CallerAssertions | None = None,
    ) -> AuthorizationDecision:
        """Return a successful decision or raise a content-minimized error."""

        decision = self.decide(
            principal,
            capability,
            resource,
            assertions=assertions,
        )
        if not decision.allowed:
            raise AuthorizationError("authorization denied")
        return decision

    def require_founder_action(
        self,
        principal: object,
        resource: ResourceScope,
        *,
        action: str,
        assertions: CallerAssertions | None = None,
    ) -> AuthorizationDecision:
        """Gate one explicitly classified irreversible action on founder authority."""

        if (
            not isinstance(action, str)
            or not action
            or action != action.strip()
            or len(action) > 256
        ):
            raise AuthorizationError("authorization denied")
        return self.require(
            principal,
            Capability.APPROVE_IRREVERSIBLE_ACTION,
            resource,
            assertions=assertions,
        )

    def require_independent_evaluator(
        self,
        principal: object,
        resource: ResourceScope,
        *,
        executed_by: str | Collection[str],
        assertions: CallerAssertions | None = None,
    ) -> AuthorizationDecision:
        """Require evaluation authority and a stable principal distinct from executors."""

        decision = self.require(
            principal,
            Capability.EVALUATION_SUBMIT,
            resource,
            assertions=assertions,
        )
        verified: VerifiedPrincipal = principal
        if isinstance(executed_by, str):
            executor_ids = (executed_by,)
        elif isinstance(executed_by, Collection):
            executor_ids = tuple(executed_by)
        else:
            raise AuthorizationError("authorization denied")
        if not executor_ids or any(
            not isinstance(executor_id, str) or not executor_id for executor_id in executor_ids
        ):
            raise AuthorizationError("authorization denied")
        if verified.principal_id in executor_ids:
            raise SeparationOfDutiesError("execution and evaluation require different principals")
        return decision


Policy = AuthorizationPolicy


__all__ = [
    "FOUNDER_ONLY_CAPABILITIES",
    "ROLE_CAPABILITIES",
    "AuthorizationDecision",
    "AuthorizationPolicy",
    "CallerAssertions",
    "Capability",
    "Policy",
    "ResourceScope",
    "Role",
]

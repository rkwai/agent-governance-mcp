"""Trusted authentication context for the deterministic substrate.

Tool payloads are untrusted data.  They never construct a principal, select a role,
or choose a company.  An authentication boundary resolves an opaque credential (or
an equivalent server-owned binding) to the immutable context defined here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from .errors import AuthorizationError

MAX_IDENTIFIER_LENGTH = 256
AGENT_RUN_EXPIRY_WARNING_WINDOW = timedelta(hours=72)
_VERIFICATION_MARKER = object()


def rfc3339_utc(value: datetime) -> str:
    """Format an aware timestamp as a canonical UTC RFC 3339 string."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be a timezone-aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class AuthenticationError(AuthorizationError):
    """An opaque credential did not resolve to an active trusted principal."""

    code = "unauthenticated"


class AgentRunAuthError(AuthenticationError):
    """The caller's verified subject is bound, but that agent run is not usable."""

    def __init__(
        self,
        message: str,
        *,
        agent_run_id: str,
        expires_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.agent_run_id = _validated_identifier(agent_run_id, "agent_run_id")
        self.expires_at = rfc3339_utc(expires_at) if expires_at is not None else None

    @property
    def details(self) -> dict[str, str]:
        payload = {"agent_run_id": self.agent_run_id}
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        return payload


class AgentRunExpiredError(AgentRunAuthError):
    code = "run_expired"


class AgentRunRevokedError(AgentRunAuthError):
    code = "run_revoked"


class AgentRunSuspendedError(AgentRunAuthError):
    code = "run_suspended"


class AgentRunInactiveError(AgentRunAuthError):
    code = "run_inactive"


def inactive_agent_run_error(
    *,
    agent_run_id: str,
    run_status: str,
    instance_status: str,
    expires_at: datetime | None,
    now: datetime | None = None,
) -> AgentRunAuthError | None:
    """Return a caller-owned run error, or None when the binding is still live."""

    observed = datetime.now(UTC) if now is None else now
    if observed.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    if instance_status == "suspended":
        return AgentRunSuspendedError(
            f"agent run {agent_run_id} is suspended",
            agent_run_id=agent_run_id,
            expires_at=expires_at,
        )
    if run_status == "revoked":
        return AgentRunRevokedError(
            f"agent run {agent_run_id} is revoked",
            agent_run_id=agent_run_id,
            expires_at=expires_at,
        )
    expired = run_status == "expired" or (
        expires_at is not None and expires_at.astimezone(UTC) <= observed.astimezone(UTC)
    )
    if expired:
        expiry_text = rfc3339_utc(expires_at) if expires_at is not None else "an unknown time"
        return AgentRunExpiredError(
            f"agent run {agent_run_id} expired at {expiry_text}",
            agent_run_id=agent_run_id,
            expires_at=expires_at,
        )
    if run_status != "running" or instance_status != "active":
        return AgentRunInactiveError(
            f"agent run {agent_run_id} is {run_status}",
            agent_run_id=agent_run_id,
            expires_at=expires_at,
        )
    return None


class Role(StrEnum):
    """Phase 1 duties; a role has no authority outside a scoped server-side grant."""

    FOUNDER = "founder"
    CELL_OWNER = "cell_owner"
    OWNER = "cell_owner"  # Compatibility alias; it does not create another role.
    SPECIALIST = "specialist"
    EVALUATOR = "evaluator"
    ALLOCATOR = "allocator"
    IMMUNE = "immune"
    OBSERVER = "observer"


class ResourceType(StrEnum):
    """Canonical resource namespaces used by scoped grants."""

    COMPANY = "company"
    CONSTITUTION = "constitution"
    AGENT = "agent"
    CELL = "cell"
    OFFER = "offer"
    EXPERIMENT = "experiment"
    RUN = "run"
    WORK_ITEM = "work_item"
    EVIDENCE = "evidence"
    EVALUATION = "evaluation"
    BUDGET = "budget"
    ALLOCATION = "allocation"
    EXTERNAL_ACTION = "external_action"
    AUDIT = "audit"
    DECISION = "decision"
    LEARNING = "learning"
    POLICY = "policy"


def _validated_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty canonical identifier")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field_name} exceeds {MAX_IDENTIFIER_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} cannot contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """A company root or one exact company-owned resource.

    Company scopes contain every resource in that company.  Resource scopes match
    only the exact resource.  Parent/child relationships must be resolved by trusted
    domain code and represented by the resource against which policy is checked;
    an empty resource never behaves as a wildcard.
    """

    company_id: str
    resource_type: ResourceType | None = None
    resource_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "company_id",
            _validated_identifier(self.company_id, "company_id"),
        )
        if (self.resource_type is None) != (self.resource_id is None):
            raise ValueError("resource_type and resource_id must be supplied together")
        if self.resource_type is None:
            return
        try:
            resource_type = ResourceType(self.resource_type)
        except (TypeError, ValueError) as error:
            raise ValueError("resource_type is unknown") from error
        if resource_type is ResourceType.COMPANY:
            raise ValueError("use the canonical company scope instead of a company resource")
        object.__setattr__(self, "resource_type", resource_type)
        object.__setattr__(
            self,
            "resource_id",
            _validated_identifier(self.resource_id, "resource_id"),
        )

    @classmethod
    def company(cls, company_id: str) -> ResourceScope:
        """Build the canonical company-wide scope."""

        return cls(company_id=company_id)

    @classmethod
    def resource(
        cls,
        company_id: str,
        resource_type: ResourceType | str,
        resource_id: str,
    ) -> ResourceScope:
        """Build an exact resource scope."""

        return cls(
            company_id=company_id,
            resource_type=resource_type,  # type: ignore[arg-type]
            resource_id=resource_id,
        )

    @property
    def is_company(self) -> bool:
        return self.resource_type is None

    def contains(self, target: object) -> bool:
        """Return whether this grant scope contains a trusted target scope."""

        if not isinstance(target, ResourceScope) or self.company_id != target.company_id:
            return False
        if self.is_company:
            return True
        return self.resource_type is target.resource_type and self.resource_id == target.resource_id


@dataclass(frozen=True, slots=True)
class ScopedRoleGrant:
    """A role assignment attenuated to one company or exact resource."""

    role: Role
    scope: ResourceScope

    def __post_init__(self) -> None:
        try:
            role = Role(self.role)
        except (TypeError, ValueError) as error:
            raise ValueError("role is unknown") from error
        if not isinstance(self.scope, ResourceScope):
            raise TypeError("scope must be a ResourceScope")
        object.__setattr__(self, "role", role)


@dataclass(frozen=True, slots=True)
class AgentIdentityContext:
    """Server-owned attribution for one concrete externally hosted agent.

    Agent definitions are reusable reviewed contracts.  An agent instance is the
    concrete identity using one definition in one optional cell.  This context is
    issued with the verified principal so transport and audit code never need to
    trust a Bot name, connector label, channel, or tool payload for attribution.
    """

    agent_instance_id: str
    definition_id: str
    definition_version: int
    role: Role
    cell_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "agent_instance_id",
            _validated_identifier(self.agent_instance_id, "agent_instance_id"),
        )
        object.__setattr__(
            self,
            "definition_id",
            _validated_identifier(self.definition_id, "definition_id"),
        )
        if (
            isinstance(self.definition_version, bool)
            or not isinstance(self.definition_version, int)
            or self.definition_version < 1
        ):
            raise ValueError("definition_version must be a positive integer")
        try:
            role = Role(self.role)
        except (TypeError, ValueError) as error:
            raise ValueError("agent identity role is unknown") from error
        object.__setattr__(self, "role", role)
        if self.cell_id is not None:
            object.__setattr__(
                self,
                "cell_id",
                _validated_identifier(self.cell_id, "cell_id"),
            )


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    """Trusted authentication configuration, never an MCP request model."""

    principal_id: str
    grants: tuple[ScopedRoleGrant, ...]
    disabled: bool = False
    agent_run_id: str | None = None
    agent_identity: AgentIdentityContext | None = None
    auth_subject_id: str | None = None
    agent_run_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "principal_id",
            _validated_identifier(self.principal_id, "principal_id"),
        )
        try:
            grants = tuple(self.grants)
        except TypeError as error:
            raise TypeError("grants must be an iterable of ScopedRoleGrant") from error
        if not all(isinstance(grant, ScopedRoleGrant) for grant in grants):
            raise TypeError("every grant must be a ScopedRoleGrant")
        if len(set(grants)) != len(grants):
            raise ValueError("duplicate grant")
        if not isinstance(self.disabled, bool):
            raise TypeError("disabled must be a boolean")
        if self.agent_run_id is not None:
            object.__setattr__(
                self,
                "agent_run_id",
                _validated_identifier(self.agent_run_id, "agent_run_id"),
            )
        if self.agent_identity is not None:
            if not isinstance(self.agent_identity, AgentIdentityContext):
                raise TypeError("agent_identity must be an AgentIdentityContext")
            if {grant.role for grant in grants} != {self.agent_identity.role}:
                raise ValueError(
                    "agent identity role must be the principal's only server-owned role"
                )
        if self.auth_subject_id is not None:
            object.__setattr__(
                self,
                "auth_subject_id",
                _validated_identifier(self.auth_subject_id, "auth_subject_id"),
            )
        if self.agent_run_expires_at is not None:
            if (
                not isinstance(self.agent_run_expires_at, datetime)
                or self.agent_run_expires_at.tzinfo is None
            ):
                raise ValueError("agent_run_expires_at must be a timezone-aware datetime")
            object.__setattr__(
                self,
                "agent_run_expires_at",
                self.agent_run_expires_at.astimezone(UTC),
            )
        object.__setattr__(self, "grants", grants)


@dataclass(frozen=True, slots=True, init=False)
class VerifiedPrincipal:
    """Immutable authority issued only after a trusted boundary authenticates.

    The private marker prevents ordinary request deserialization or duck typing from
    turning actor/role/company fields into authority.  Python code within this trusted
    process remains part of the security boundary, as with any in-process policy API.
    """

    principal_id: str
    grants: tuple[ScopedRoleGrant, ...]
    authentication_method: str
    authentication_id: str
    agent_run_id: str | None
    agent_identity: AgentIdentityContext | None
    auth_subject_id: str | None
    agent_run_expires_at: datetime | None
    __verification_marker: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("VerifiedPrincipal instances must be issued by an authentication boundary")

    @classmethod
    def _issue(
        cls,
        record: PrincipalRecord,
        *,
        authentication_method: str,
        authentication_id: str,
        marker: object,
    ) -> VerifiedPrincipal:
        if marker is not _VERIFICATION_MARKER:
            raise AuthenticationError("authentication failed")
        instance = object.__new__(cls)
        object.__setattr__(instance, "principal_id", record.principal_id)
        object.__setattr__(instance, "grants", record.grants)
        object.__setattr__(
            instance,
            "authentication_method",
            _validated_identifier(authentication_method, "authentication_method"),
        )
        object.__setattr__(
            instance,
            "authentication_id",
            _validated_identifier(authentication_id, "authentication_id"),
        )
        object.__setattr__(instance, "agent_run_id", record.agent_run_id)
        object.__setattr__(instance, "agent_identity", record.agent_identity)
        object.__setattr__(instance, "auth_subject_id", record.auth_subject_id)
        object.__setattr__(instance, "agent_run_expires_at", record.agent_run_expires_at)
        object.__setattr__(instance, "_VerifiedPrincipal__verification_marker", marker)
        return instance

    def caller_agent_run_view(self, *, now: datetime) -> dict[str, object]:
        """Return this principal's own run expiry view; never another binding."""

        expires_at = self.agent_run_expires_at
        expires_soon = False
        if expires_at is not None:
            if now.tzinfo is None:
                raise ValueError("now must be a timezone-aware datetime")
            remaining = expires_at - now.astimezone(UTC)
            expires_soon = timedelta(0) < remaining <= AGENT_RUN_EXPIRY_WARNING_WINDOW
        return {
            "agent_run_id": self.agent_run_id,
            "expires_at": rfc3339_utc(expires_at) if expires_at is not None else None,
            "expires_soon": expires_soon,
        }

    @property
    def roles(self) -> frozenset[Role]:
        return frozenset(grant.role for grant in self.grants)

    @property
    def company_ids(self) -> frozenset[str]:
        return frozenset(grant.scope.company_id for grant in self.grants)

    @property
    def company_id(self) -> str:
        """Return the company for a single-company binding.

        Code supporting multi-company identities must use ``company_ids`` and an
        explicitly authorized resource instead of choosing one implicitly.
        """

        company_ids = self.company_ids
        if len(company_ids) != 1:
            raise ValueError("principal is not bound to exactly one company")
        return next(iter(company_ids))

    def grants_for(self, resource: ResourceScope) -> tuple[ScopedRoleGrant, ...]:
        if not isinstance(resource, ResourceScope):
            return ()
        return tuple(grant for grant in self.grants if grant.scope.contains(resource))


def is_verified_principal(value: object) -> bool:
    """Check provenance without trusting a caller-compatible object shape."""

    return isinstance(value, VerifiedPrincipal) and (
        getattr(value, "_VerifiedPrincipal__verification_marker", None) is _VERIFICATION_MARKER
    )


@runtime_checkable
class AuthenticationBoundary(Protocol):
    """Adapter boundary for local credentials or an external verified token."""

    def authenticate(self, credential: str) -> VerifiedPrincipal:
        """Resolve an opaque credential to immutable verified authority."""


class LocalTestAuthenticator:
    """In-memory credential binding for tests and trusted local/stdio startup.

    This class performs no cryptographic token verification and must not be used as
    an internet-facing bearer-token verifier.  Its registry is trusted configuration,
    copied at construction, and is never exposed in representations or failures.
    """

    __slots__ = ("_records",)

    def __init__(self, records: Mapping[str, PrincipalRecord]) -> None:
        if not isinstance(records, Mapping):
            raise TypeError("records must be a credential-to-principal mapping")
        copied: dict[str, PrincipalRecord] = {}
        for credential, record in records.items():
            _validated_identifier(credential, "credential")
            if not isinstance(record, PrincipalRecord):
                raise TypeError("each credential must map to a PrincipalRecord")
            copied[credential] = record
        self._records = MappingProxyType(copied)

    def authenticate(self, credential: str) -> VerifiedPrincipal:
        record = self._records.get(credential) if isinstance(credential, str) else None
        if record is None or record.disabled:
            raise AuthenticationError("authentication failed")
        return VerifiedPrincipal._issue(
            record,
            authentication_method="local-test",
            authentication_id=f"local-test:{record.principal_id}",
            marker=_VERIFICATION_MARKER,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} identities={len(self._records)}>"


__all__ = [
    "AGENT_RUN_EXPIRY_WARNING_WINDOW",
    "AgentIdentityContext",
    "AgentRunAuthError",
    "AgentRunExpiredError",
    "AgentRunInactiveError",
    "AgentRunRevokedError",
    "AgentRunSuspendedError",
    "AuthenticationBoundary",
    "AuthenticationError",
    "LocalTestAuthenticator",
    "PrincipalRecord",
    "ResourceScope",
    "ResourceType",
    "Role",
    "ScopedRoleGrant",
    "VerifiedPrincipal",
    "inactive_agent_run_error",
    "is_verified_principal",
    "rfc3339_utc",
]

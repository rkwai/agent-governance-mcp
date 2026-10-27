"""Supabase OAuth/JWT adapter for the hosted MCP authentication boundary.

The JWT verifier establishes only an external OAuth identity.  Application
authority is always loaded from a server-owned subject binding; token claims never
select a principal, role, company, cell, definition, or agent run.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

from . import auth as trusted_auth
from .auth import (
    AgentRunAuthError,
    AuthenticationError,
    PrincipalRecord,
    Role,
    VerifiedPrincipal,
    inactive_agent_run_error,
)
from .mcp_resource import is_local_mcp_resource_url, request_mcp_resource_url

MAX_BEARER_TOKEN_BYTES = 16_384
MAX_EXTERNAL_IDENTIFIER_LENGTH = 512
_ASYMMETRIC_ALGORITHMS = frozenset({"RS256", "ES256"})
_REQUIRED_CLAIMS = ("iss", "aud", "sub", "session_id", "iat", "exp")


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty canonical identifier")
    if len(value) > MAX_EXTERNAL_IDENTIFIER_LENGTH:
        raise ValueError(f"{field_name} exceeds {MAX_EXTERNAL_IDENTIFIER_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} cannot contain control characters")
    return value


def _absolute_http_url(value: object, field_name: str) -> str:
    canonical = _identifier(value, field_name)
    parsed = urlsplit(canonical)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain a query or fragment")
    return canonical


def _numeric_date(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer NumericDate")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    return value


def _uuid_identifier(value: object, field_name: str) -> str:
    canonical = _identifier(value, field_name)
    try:
        parsed = UUID(canonical)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a canonical UUID") from error
    if str(parsed) != canonical:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return canonical


def _scopes(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split()
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw = value
    else:
        raise ValueError("scope must be a string or list of strings")
    result: list[str] = []
    for scope in raw:
        canonical = _identifier(scope, "scope")
        if canonical not in result:
            result.append(canonical)
    return result


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SupabaseJWTSettings:
    """Strict verification configuration for one Supabase Auth issuer."""

    issuer: str
    audience: str
    resource_server_url: str
    allowed_client_ids: frozenset[str] = frozenset()
    jwks_url: str | None = None
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    leeway_seconds: int = 30

    def __post_init__(self) -> None:
        issuer = _absolute_http_url(self.issuer, "issuer")
        audience = _identifier(self.audience, "audience")
        resource_server_url = _absolute_http_url(
            self.resource_server_url,
            "resource_server_url",
        )
        try:
            client_ids = frozenset(
                _identifier(client_id, "allowed_client_id") for client_id in self.allowed_client_ids
            )
        except TypeError as error:
            raise TypeError("allowed_client_ids must be an iterable of strings") from error
        try:
            algorithms = tuple(self.algorithms)
        except TypeError as error:
            raise TypeError("algorithms must be an iterable of strings") from error
        if not algorithms or len(set(algorithms)) != len(algorithms):
            raise ValueError("algorithms must contain unique asymmetric algorithms")
        if not set(algorithms) <= _ASYMMETRIC_ALGORITHMS:
            raise ValueError("algorithms may contain only supported asymmetric algorithms")
        if (
            isinstance(self.leeway_seconds, bool)
            or not isinstance(self.leeway_seconds, int)
            or not 0 <= self.leeway_seconds <= 300
        ):
            raise ValueError("leeway_seconds must be an integer from 0 through 300")
        jwks_url = (
            _absolute_http_url(self.jwks_url, "jwks_url")
            if self.jwks_url is not None
            else f"{issuer.rstrip('/')}/.well-known/jwks.json"
        )
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "audience", audience)
        object.__setattr__(self, "resource_server_url", resource_server_url)
        object.__setattr__(self, "allowed_client_ids", client_ids)
        object.__setattr__(self, "algorithms", algorithms)
        object.__setattr__(self, "jwks_url", jwks_url)


@runtime_checkable
class SigningKeyProvider(Protocol):
    """Resolve the trusted verification key selected by a signed JWT header."""

    async def signing_key(self, token: str) -> object:
        """Return a cryptographic public key without treating claims as authority."""


class SupabaseJWKSProvider:
    """Cached JWKS key selection using PyJWT's TLS-validating client."""

    __slots__ = ("_client",)

    def __init__(self, jwks_url: str) -> None:
        self._client = PyJWKClient(
            _absolute_http_url(jwks_url, "jwks_url"),
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,
            timeout=10,
        )

    async def signing_key(self, token: str) -> object:
        key = await asyncio.to_thread(self._client.get_signing_key_from_jwt, token)
        return key.key

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


class SupabaseJWTVerifier:
    """MCP SDK ``TokenVerifier`` backed by Supabase's asymmetric JWKS."""

    __slots__ = ("_settings", "_signing_keys")

    def __init__(
        self,
        settings: SupabaseJWTSettings,
        *,
        signing_keys: SigningKeyProvider | None = None,
    ) -> None:
        if not isinstance(settings, SupabaseJWTSettings):
            raise TypeError("settings must be SupabaseJWTSettings")
        if signing_keys is not None and not isinstance(signing_keys, SigningKeyProvider):
            raise TypeError("signing_keys must implement SigningKeyProvider")
        self._settings = settings
        self._signing_keys = signing_keys or SupabaseJWKSProvider(settings.jwks_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token and return only content-minimized trusted claims."""

        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > MAX_BEARER_TOKEN_BYTES
        ):
            return None
        try:
            header = jwt.get_unverified_header(token)
            if not isinstance(header, Mapping):
                return None
            algorithm = _identifier(header.get("alg"), "alg")
            _identifier(header.get("kid"), "kid")
            if algorithm not in self._settings.algorithms:
                return None
            signing_key = await self._signing_keys.signing_key(token)
            decoded = jwt.decode(
                token,
                signing_key,
                algorithms=list(self._settings.algorithms),
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=self._settings.leeway_seconds,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
            if not isinstance(decoded, Mapping):
                return None
            issuer = _absolute_http_url(decoded.get("iss"), "iss")
            if issuer != self._settings.issuer:
                return None
            if decoded.get("aud") != self._settings.audience:
                return None
            subject = _identifier(decoded.get("sub"), "sub")
            raw_client_id = decoded.get("client_id")
            raw_authorized_party = decoded.get("azp")
            if raw_client_id is None and raw_authorized_party is None:
                return None
            client_id = _identifier(
                raw_client_id if raw_client_id is not None else raw_authorized_party,
                "client_id",
            )
            if raw_authorized_party is not None:
                authorized_party = _identifier(raw_authorized_party, "azp")
                if authorized_party != client_id:
                    return None
            if (
                self._settings.allowed_client_ids
                and client_id not in self._settings.allowed_client_ids
            ):
                return None
            issued_at = _numeric_date(decoded.get("iat"), "iat")
            expires_at = _numeric_date(decoded.get("exp"), "exp")
            session_id = _uuid_identifier(decoded.get("session_id"), "session_id")
            scopes = _scopes(decoded.get("scope"))
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=expires_at,
                subject=subject,
                resource=self._settings.resource_server_url,
                claims={
                    "iss": issuer,
                    "aud": decoded["aud"],
                    "iat": issued_at,
                    "session_id": session_id,
                },
            )
        except (jwt.PyJWTError, TypeError, ValueError, UnicodeError):
            return None

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} issuer={self._settings.issuer!r} "
            f"clients={len(self._settings.allowed_client_ids)}>"
        )


@dataclass(frozen=True, slots=True)
class SubjectIdentityBinding:
    """Persisted external subject binding and its active agent authority snapshot."""

    issuer: str
    subject: str
    client_id: str
    principal: PrincipalRecord
    auth_subject_id: str
    agent_instance_id: str
    definition_id: str
    definition_version: int
    cell_id: str | None
    assigned_role: Role
    agent_run_id: str
    token_valid_after: datetime | None = None
    run_expires_at: datetime | None = None
    run_status: str = "running"
    instance_status: str = "active"

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _absolute_http_url(self.issuer, "issuer"))
        object.__setattr__(self, "subject", _identifier(self.subject, "subject"))
        object.__setattr__(self, "client_id", _identifier(self.client_id, "client_id"))
        if not isinstance(self.principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord")
        auth_subject_id = _identifier(self.auth_subject_id, "auth_subject_id")
        if self.principal.auth_subject_id != auth_subject_id:
            raise ValueError("principal auth subject must exactly match the subject binding")
        object.__setattr__(self, "auth_subject_id", auth_subject_id)
        object.__setattr__(
            self,
            "agent_instance_id",
            _identifier(self.agent_instance_id, "agent_instance_id"),
        )
        object.__setattr__(self, "definition_id", _identifier(self.definition_id, "definition_id"))
        if (
            isinstance(self.definition_version, bool)
            or not isinstance(self.definition_version, int)
            or self.definition_version < 1
        ):
            raise ValueError("definition_version must be a positive integer")
        if self.cell_id is not None:
            object.__setattr__(self, "cell_id", _identifier(self.cell_id, "cell_id"))
        try:
            assigned_role = Role(self.assigned_role)
        except (TypeError, ValueError) as error:
            raise ValueError("assigned_role is unknown") from error
        principal_roles = {grant.role for grant in self.principal.grants}
        if principal_roles != {assigned_role}:
            raise ValueError("assigned_role must be the principal's only granted role")
        object.__setattr__(self, "assigned_role", assigned_role)
        expected_identity = trusted_auth.AgentIdentityContext(
            agent_instance_id=self.agent_instance_id,
            definition_id=self.definition_id,
            definition_version=self.definition_version,
            role=assigned_role,
            cell_id=self.cell_id,
        )
        if self.principal.agent_identity != expected_identity:
            raise ValueError("principal agent identity must exactly match the subject binding")
        agent_run_id = _identifier(self.agent_run_id, "agent_run_id")
        if self.principal.agent_run_id != agent_run_id:
            raise ValueError("principal agent_run_id must match the active agent run")
        object.__setattr__(self, "agent_run_id", agent_run_id)
        if self.token_valid_after is not None:
            object.__setattr__(
                self,
                "token_valid_after",
                _aware_utc(self.token_valid_after, "token_valid_after"),
            )
        if self.run_expires_at is not None:
            object.__setattr__(
                self,
                "run_expires_at",
                _aware_utc(self.run_expires_at, "run_expires_at"),
            )
        run_status = _identifier(self.run_status, "run_status")
        if run_status not in {
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "revoked",
            "expired",
        }:
            raise ValueError("run_status is unknown")
        object.__setattr__(self, "run_status", run_status)
        instance_status = _identifier(self.instance_status, "instance_status")
        if instance_status not in {"active", "suspended", "retired"}:
            raise ValueError("instance_status is unknown")
        object.__setattr__(self, "instance_status", instance_status)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.issuer, self.subject, self.client_id)


@runtime_checkable
class SubjectIdentityRepository(Protocol):
    """Trusted, pre-company lookup for one verified external OAuth subject."""

    async def resolve(
        self,
        *,
        issuer: str,
        subject: str,
        client_id: str,
        resource_server_url: str,
        session_id: str,
    ) -> SubjectIdentityBinding | None:
        """Return the one active server-owned binding for an exact identity tuple."""


class InMemorySubjectIdentityRepository:
    """Immutable subject registry for tests and local integration only."""

    __slots__ = ("_bindings",)

    def __init__(self, bindings: Iterable[SubjectIdentityBinding]) -> None:
        copied: dict[tuple[str, str, str], SubjectIdentityBinding] = {}
        try:
            iterable = tuple(bindings)
        except TypeError as error:
            raise TypeError("bindings must be an iterable of SubjectIdentityBinding") from error
        for binding in iterable:
            if not isinstance(binding, SubjectIdentityBinding):
                raise TypeError("each binding must be a SubjectIdentityBinding")
            if binding.key in copied:
                raise ValueError("duplicate subject identity binding")
            copied[binding.key] = binding
        self._bindings = MappingProxyType(copied)

    async def resolve(
        self,
        *,
        issuer: str,
        subject: str,
        client_id: str,
        resource_server_url: str,
        session_id: str,
    ) -> SubjectIdentityBinding | None:
        try:
            _absolute_http_url(resource_server_url, "resource_server_url")
            _uuid_identifier(session_id, "session_id")
            key = (
                _absolute_http_url(issuer, "issuer"),
                _identifier(subject, "subject"),
                _identifier(client_id, "client_id"),
            )
        except (TypeError, ValueError):
            return None
        return self._bindings.get(key)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} identities={len(self._bindings)}>"


class SupabaseIdentityAuthenticator:
    """Map an SDK-verified Supabase token to immutable substrate authority."""

    __slots__ = ("_repository", "_resource_server_url")

    def __init__(
        self,
        repository: SubjectIdentityRepository,
        *,
        resource_server_url: str,
    ) -> None:
        if not isinstance(repository, SubjectIdentityRepository):
            raise TypeError("repository must implement SubjectIdentityRepository")
        self._repository = repository
        self._resource_server_url = _absolute_http_url(
            resource_server_url,
            "resource_server_url",
        )

    async def __call__(self, access_token: AccessToken) -> VerifiedPrincipal:
        try:
            if not isinstance(access_token, AccessToken):
                raise ValueError("access token is not SDK verified")
            claims = access_token.claims
            if not isinstance(claims, Mapping):
                raise ValueError("verified issuer claims are missing")
            issuer = _absolute_http_url(claims.get("iss"), "iss")
            subject = _identifier(access_token.subject, "subject")
            client_id = _identifier(access_token.client_id, "client_id")
            request_resource = request_mcp_resource_url()
            resource = (
                request_resource
                if request_resource is not None
                else _absolute_http_url(access_token.resource, "resource")
            )
            if not is_local_mcp_resource_url(self._resource_server_url, resource):
                raise ValueError("access token is for another protected resource")
            issued_at = _numeric_date(claims.get("iat"), "iat")
            session_id = _uuid_identifier(claims.get("session_id"), "session_id")
            binding = await self._repository.resolve(
                issuer=issuer,
                subject=subject,
                client_id=client_id,
                resource_server_url=resource,
                session_id=session_id,
            )
            if binding is None or binding.key != (issuer, subject, client_id):
                raise ValueError("identity is not bound")
            if binding.principal.disabled:
                raise ValueError("identity is disabled")
            if binding.token_valid_after is not None:
                token_issued_at = datetime.fromtimestamp(issued_at, UTC)
                if token_issued_at < binding.token_valid_after:
                    raise ValueError("token was issued before revocation")
            inactive = inactive_agent_run_error(
                agent_run_id=binding.agent_run_id,
                run_status=binding.run_status,
                instance_status=binding.instance_status,
                expires_at=binding.run_expires_at,
            )
            if inactive is not None:
                raise inactive
        except AgentRunAuthError:
            raise
        except (TypeError, ValueError, OverflowError):
            raise AuthenticationError("authentication failed") from None

        digest = hashlib.sha256(f"{issuer}\0{subject}\0{client_id}".encode()).hexdigest()
        record = binding.principal
        if record.agent_run_expires_at is None and binding.run_expires_at is not None:
            record = replace(record, agent_run_expires_at=binding.run_expires_at)
        # This adapter is an explicit trusted authentication boundary.  The marker
        # remains private to auth.py so request models cannot construct authority.
        return trusted_auth.VerifiedPrincipal._issue(
            record,
            authentication_method="supabase-oauth",
            authentication_id=f"supabase:{digest}",
            marker=trusted_auth._VERIFICATION_MARKER,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


__all__ = [
    "MAX_BEARER_TOKEN_BYTES",
    "InMemorySubjectIdentityRepository",
    "SigningKeyProvider",
    "SubjectIdentityBinding",
    "SubjectIdentityRepository",
    "SupabaseIdentityAuthenticator",
    "SupabaseJWKSProvider",
    "SupabaseJWTSettings",
    "SupabaseJWTVerifier",
]

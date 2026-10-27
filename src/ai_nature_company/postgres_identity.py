"""PostgreSQL subject resolver for the hosted Supabase authentication path.

This adapter operates before a company is known.  It receives only claims already
verified by :mod:`ai_nature_company.supabase_auth` and calls the migration-owned,
session-aware resolver function.  The runtime database role has EXECUTE permission
on that function but no direct SELECT permission on identity tables.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .auth import AgentIdentityContext, PrincipalRecord, ResourceScope, Role, ScopedRoleGrant
from .supabase_auth import (
    SubjectIdentityBinding,
    _absolute_http_url,
    _identifier,
    _uuid_identifier,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

try:
    from psycopg_pool import AsyncConnectionPool as PsycopgAsyncConnectionPool
except ImportError:  # pragma: no cover - exercised only in dependency-broken installs
    PsycopgAsyncConnectionPool = None

_RESOLVED_FIELDS = (
    "company_id",
    "auth_subject_id",
    "principal_id",
    "subject_kind",
    "token_valid_after",
    "agent_instance_id",
    "definition_id",
    "definition_version",
    "assigned_role",
    "cell_id",
    "agent_run_id",
    "run_expires_at",
)
_INSPECTED_FIELDS = (
    *_RESOLVED_FIELDS,
    "run_status",
    "instance_status",
)


@dataclass(frozen=True, slots=True)
class ExpiringAgentRun:
    """One caller-visible running agent run that expires within the warning window."""

    agent_run_id: str
    agent_instance_id: str
    expires_at: datetime


def format_expiring_agent_run_warning(runs: Sequence[ExpiringAgentRun]) -> str | None:
    """Build a content-minimized warning that names only run and instance ids."""

    if not runs:
        return None
    parts = []
    for run in runs:
        expiry = run.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        parts.append(f"run={run.agent_run_id} instance={run.agent_instance_id} expires_at={expiry}")
    return "agent runs expire within 72h: " + "; ".join(parts)


async def fetch_expiring_agent_runs(connection: Any) -> tuple[ExpiringAgentRun, ...]:
    """List running agent runs that expire within 72 hours through the definer."""

    cursor = await connection.execute(
        """
        SELECT agent_run_id, agent_instance_id, expires_at
        FROM public.anc_list_expiring_agent_runs()
        """
    )
    rows = await cursor.fetchall()
    found: list[ExpiringAgentRun] = []
    for row in rows:
        agent_run_id, agent_instance_id, expires_at = row
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            continue
        found.append(
            ExpiringAgentRun(
                agent_run_id=_identifier(agent_run_id, "agent_run_id"),
                agent_instance_id=_identifier(agent_instance_id, "agent_instance_id"),
                expires_at=expires_at.astimezone(UTC),
            )
        )
    return tuple(found)


class PostgresSubjectIdentityRepository:
    """Resolve an exact OAuth subject to one active agent instance and run."""

    __slots__ = ("_owns_pool", "_pool")

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: AsyncConnectionPool | None = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        if pool is None:
            if PsycopgAsyncConnectionPool is None:
                raise RuntimeError(
                    "PostgresSubjectIdentityRepository requires psycopg[binary,pool]"
                )
            if not dsn:
                raise ValueError("dsn is required when pool is not supplied")
            pool = PsycopgAsyncConnectionPool(
                conninfo=dsn,
                min_size=min_size,
                max_size=max_size,
                open=False,
            )
            self._owns_pool = True
        else:
            self._owns_pool = False
        self._pool = pool

    async def __aenter__(self) -> PostgresSubjectIdentityRepository:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()

    async def open(self) -> None:
        if getattr(self._pool, "closed", False):
            await self._pool.open()

    async def close(self) -> None:
        if self._owns_pool and not getattr(self._pool, "closed", True):
            await self._pool.close()

    async def resolve(
        self,
        *,
        issuer: str,
        subject: str,
        client_id: str,
        resource_server_url: str,
        session_id: str,
    ) -> SubjectIdentityBinding | None:
        issuer = _absolute_http_url(issuer, "issuer")
        subject = _identifier(subject, "subject")
        client_id = _identifier(client_id, "client_id")
        resource_server_url = _absolute_http_url(
            resource_server_url,
            "resource_server_url",
        )
        session_id = _uuid_identifier(session_id, "session_id")
        parameters = (issuer, subject, client_id, resource_server_url, session_id)
        await self.open()
        async with self._pool.connection() as connection, connection.transaction():
            safety_cursor = await connection.execute(
                """
                SELECT NOT (role.rolsuper OR role.rolbypassrls) AS runtime_role_is_safe
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = current_user
                """
            )
            safety = await safety_cursor.fetchone()
            if safety is None or safety[0] is not True:
                raise RuntimeError("database runtime role must be NOSUPERUSER and NOBYPASSRLS")
            cursor = await connection.execute(
                """
                SELECT company_id, auth_subject_id, principal_id, subject_kind,
                       token_valid_after, agent_instance_id, definition_id,
                       definition_version, assigned_role, cell_id, agent_run_id,
                       run_expires_at
                FROM public.anc_resolve_agent_identity(%s, %s, %s, %s, %s)
                """,
                parameters,
            )
            rows = await cursor.fetchall()
            if not rows:
                inspect_cursor = await connection.execute(
                    """
                    SELECT company_id, auth_subject_id, principal_id, subject_kind,
                           token_valid_after, agent_instance_id, definition_id,
                           definition_version, assigned_role, cell_id, agent_run_id,
                           run_expires_at, run_status, instance_status
                    FROM public.anc_inspect_agent_run_binding(%s, %s, %s, %s, %s)
                    """,
                    parameters,
                )
                rows = await inspect_cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("identity resolver returned an ambiguous binding")
        return self._binding_from_row(
            rows[0],
            issuer=issuer,
            subject=subject,
            client_id=client_id,
        )

    @staticmethod
    def _binding_from_row(
        row: tuple[Any, ...] | Mapping[str, Any],
        *,
        issuer: str,
        subject: str,
        client_id: str,
    ) -> SubjectIdentityBinding | None:
        if isinstance(row, Mapping):
            field_names = (
                _INSPECTED_FIELDS
                if all(field in row for field in _INSPECTED_FIELDS)
                else _RESOLVED_FIELDS
            )
            try:
                row = tuple(row[field] for field in field_names)
            except KeyError as error:
                raise RuntimeError("identity resolver returned an invalid record") from error
        if len(row) == len(_RESOLVED_FIELDS):
            row = (*row, "running", "active")
        if len(row) != len(_INSPECTED_FIELDS):
            raise RuntimeError("identity resolver returned an invalid record")
        (
            company_id,
            auth_subject_id,
            principal_id,
            subject_kind,
            token_valid_after,
            agent_instance_id,
            definition_id,
            definition_version,
            assigned_role,
            cell_id,
            agent_run_id,
            run_expires_at,
            run_status,
            instance_status,
        ) = row
        normalized_auth_subject_id = _identifier(auth_subject_id, "auth_subject_id")
        if subject_kind != "agent":
            raise ValueError("resolved subject is not an agent")
        if not isinstance(run_expires_at, datetime) or run_expires_at.tzinfo is None:
            raise ValueError("run_expires_at must be a timezone-aware datetime")
        if token_valid_after is not None and (
            not isinstance(token_valid_after, datetime) or token_valid_after.tzinfo is None
        ):
            raise ValueError("token_valid_after must be a timezone-aware datetime")
        role = Role(assigned_role)
        company_scope = ResourceScope.company(_identifier(company_id, "company_id"))
        normalized_cell_id = _identifier(cell_id, "cell_id") if cell_id is not None else None
        agent_identity = AgentIdentityContext(
            agent_instance_id=_identifier(agent_instance_id, "agent_instance_id"),
            definition_id=_identifier(definition_id, "definition_id"),
            definition_version=definition_version,
            role=role,
            cell_id=normalized_cell_id,
        )
        active_run_id = _identifier(agent_run_id, "agent_run_id")
        expiry = run_expires_at.astimezone(UTC)
        principal = PrincipalRecord(
            principal_id=_identifier(principal_id, "principal_id"),
            grants=(ScopedRoleGrant(role, company_scope),),
            agent_run_id=active_run_id,
            agent_identity=agent_identity,
            auth_subject_id=normalized_auth_subject_id,
            agent_run_expires_at=expiry,
        )
        return SubjectIdentityBinding(
            issuer=issuer,
            subject=subject,
            client_id=client_id,
            principal=principal,
            auth_subject_id=normalized_auth_subject_id,
            agent_instance_id=agent_identity.agent_instance_id,
            definition_id=agent_identity.definition_id,
            definition_version=agent_identity.definition_version,
            cell_id=agent_identity.cell_id,
            assigned_role=role,
            agent_run_id=active_run_id,
            token_valid_after=(
                token_valid_after.astimezone(UTC) if token_valid_after is not None else None
            ),
            run_expires_at=expiry,
            run_status=_identifier(run_status, "run_status"),
            instance_status=_identifier(instance_status, "instance_status"),
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


__all__ = [
    "ExpiringAgentRun",
    "PostgresSubjectIdentityRepository",
    "fetch_expiring_agent_runs",
    "format_expiring_agent_run_warning",
]

"""Durable, append-only PostgreSQL persistence for MCP call attribution.

The transport constructs :class:`~ai_nature_company.attribution.ToolCallAuditEvent`
instances exclusively from verified server context.  This adapter maps those events
to the migration-owned ``anc_mcp_tool_calls`` lifecycle table without retaining raw
arguments, responses, bearer tokens, or hidden reasoning.

Each ``record`` call is its own database transaction.  Consequently a successful
``received`` call is committed before tool dispatch begins, while a terminal event
is appended in a later transaction and can never rewrite the received record.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from .attribution import ToolCallAuditEvent, ToolCallAuditSink, ToolCallPhase

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

try:
    from psycopg_pool import AsyncConnectionPool as PsycopgAsyncConnectionPool
except ImportError:  # pragma: no cover - exercised only in dependency-broken installs
    PsycopgAsyncConnectionPool = None

_AUTHORIZATION_ALLOWED = "authorization.allowed"
_AUTHORIZATION_DENIED = "authorization.denied"


def _transport_name(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("transport must be a non-empty, trimmed string")
    if len(value) > 64:
        raise ValueError("transport must be at most 64 characters")
    return value


def _validate_event(event: ToolCallAuditEvent) -> None:
    if not isinstance(event, ToolCallAuditEvent):
        raise TypeError("event must be a ToolCallAuditEvent")
    if not isinstance(event.phase, ToolCallPhase):
        raise ValueError("event phase must be a ToolCallPhase")
    if event.occurred_at.tzinfo is None:
        raise ValueError("event occurred_at must be timezone-aware")
    if event.phase is ToolCallPhase.RECEIVED:
        if any(
            value is not None
            for value in (
                event.duration_ms,
                event.error_code,
                event.response_sha256,
            )
        ):
            raise ValueError("received events cannot contain terminal outcome fields")
        return
    if event.duration_ms is None:
        raise ValueError("terminal events require duration_ms")
    if event.phase is ToolCallPhase.SUCCEEDED and event.error_code is not None:
        raise ValueError("succeeded events cannot contain an error_code")
    if event.phase is ToolCallPhase.FAILED and not event.error_code:
        raise ValueError("failed events require an error_code")


def _outcomes(event: ToolCallAuditEvent) -> tuple[str, str | None, str]:
    if event.phase is ToolCallPhase.RECEIVED:
        return ("pending", None, "pending")
    if event.phase is ToolCallPhase.DENIED:
        return ("denied", event.error_code or _AUTHORIZATION_DENIED, "not_executed")
    if event.phase is ToolCallPhase.SUCCEEDED:
        return ("allowed", _AUTHORIZATION_ALLOWED, "succeeded")
    return ("allowed", _AUTHORIZATION_ALLOWED, "failed")


class PostgresToolCallAuditSink(ToolCallAuditSink):
    """Append trusted MCP call lifecycle events using a safe runtime DB role.

    A supplied pool remains owned by the caller.  When constructed from ``dsn``,
    this object owns a lazily opened pool and closes it from :meth:`close` or the
    async context manager.
    """

    __slots__ = ("_owns_pool", "_pool", "_transport")

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: AsyncConnectionPool | None = None,
        transport: str = "streamable_http",
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        if pool is None:
            if PsycopgAsyncConnectionPool is None:
                raise RuntimeError("PostgresToolCallAuditSink requires psycopg[binary,pool]")
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
        self._transport = _transport_name(transport)

    async def __aenter__(self) -> PostgresToolCallAuditSink:
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

    async def record(self, event: ToolCallAuditEvent) -> None:
        """Commit one event, propagating any validation or durability failure."""

        _validate_event(event)
        await self.open()
        async with self._pool.connection() as connection, connection.transaction():
            scope_cursor = await connection.execute(
                """
                SELECT set_config('app.company_id', %s, true),
                       NOT (role.rolsuper OR role.rolbypassrls) AS rls_enforced
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = current_user
                """,
                (event.company_id,),
            )
            scope = await scope_cursor.fetchone()
            if scope is None or scope[0] != event.company_id:
                raise RuntimeError("database company scope could not be established")
            if scope[1] is not True:
                raise RuntimeError("database runtime role must be NOSUPERUSER and NOBYPASSRLS")

            received_at = await self._received_at(connection, event)
            authorization_outcome, authorization_reason, execution_outcome = _outcomes(event)
            cursor = await connection.execute(
                """
                INSERT INTO public.anc_mcp_tool_calls
                    (company_id, call_id, phase, request_id, correlation_id,
                     principal_id, auth_subject_id, authentication_method,
                     authentication_id, agent_instance_id, agent_run_id,
                     definition_id, definition_version, role, cell_id, tool_name,
                     transport, protocol_version, arguments_sha256,
                     idempotency_key_sha256, authorization_outcome,
                     authorization_reason_code, execution_outcome, error_code,
                     response_sha256, resource_type, resource_id, resource_revision,
                     received_at, recorded_at, duration_ms)
                VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s, %s,
                     %s, %s, %s,
                     %s, %s, %s, %s, %s,
                     %s, %s, %s,
                     %s, %s,
                     %s, %s, %s,
                     %s, %s, %s, %s,
                     %s, %s, %s)
                RETURNING position
                """,
                (
                    event.company_id,
                    event.call_id,
                    event.phase.value,
                    event.request_id,
                    event.correlation_id,
                    event.principal_id,
                    event.auth_subject_id,
                    event.authentication_method,
                    event.authentication_id,
                    event.agent_instance_id,
                    event.agent_run_id,
                    event.definition_id,
                    event.definition_version,
                    event.role,
                    event.cell_id,
                    event.tool_name,
                    self._transport,
                    event.protocol_version,
                    event.arguments_sha256,
                    event.idempotency_key_sha256,
                    authorization_outcome,
                    authorization_reason,
                    execution_outcome,
                    event.error_code,
                    event.response_sha256,
                    None,
                    None,
                    None,
                    received_at,
                    event.occurred_at,
                    event.duration_ms,
                ),
            )
            inserted = await cursor.fetchone()
            if inserted is None:
                raise RuntimeError("MCP tool-call event insert returned no row")

    @staticmethod
    async def _received_at(connection: Any, event: ToolCallAuditEvent) -> datetime:
        if event.phase is ToolCallPhase.RECEIVED:
            return event.occurred_at
        cursor = await connection.execute(
            """
            SELECT received_at
            FROM public.anc_mcp_tool_calls
            WHERE company_id = %s AND call_id = %s AND phase = 'received'
            """,
            (event.company_id, event.call_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("terminal MCP tool-call event requires a durable received event")
        received_at = row[0]
        if not isinstance(received_at, datetime) or received_at.tzinfo is None:
            raise RuntimeError("received MCP tool-call event has an invalid timestamp")
        return received_at


__all__ = ["PostgresToolCallAuditSink"]

"""PostgreSQL adapter contracts for pre-company OAuth subject resolution."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import pytest

from ai_nature_company.auth import AgentIdentityContext, ResourceScope, Role
from ai_nature_company.postgres_identity import (
    ExpiringAgentRun,
    PostgresSubjectIdentityRepository,
    fetch_expiring_agent_runs,
    format_expiring_agent_run_warning,
)

ISSUER = "https://project-ref.supabase.co/auth/v1"
SUBJECT = "10000000-0000-4000-8000-000000000001"
CLIENT_ID = "50000000-0000-4000-8000-000000000001"
RESOURCE = "https://substrate.example.test/mcp"
SESSION_ID = "40000000-0000-4000-8000-000000000001"


class _Context(AbstractAsyncContextManager[Any]):
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)


class _Connection:
    def __init__(
        self,
        *,
        safe_role: bool,
        rows: list[tuple[object, ...]],
        inspect_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.safe_role = safe_role
        self.rows = rows
        self.inspect_rows = [] if inspect_rows is None else inspect_rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _Context:
        return _Context(self)

    async def execute(
        self,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor:
        self.calls.append((query, parameters))
        if "pg_catalog.pg_roles" in query:
            return _Cursor([(self.safe_role,)])
        if "anc_list_expiring_agent_runs" in query:
            return _Cursor(self.rows)
        if "anc_inspect_agent_run_binding" in query:
            return _Cursor(self.inspect_rows)
        assert "anc_resolve_agent_identity" in query
        return _Cursor(self.rows)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_value = connection
        self.closed = False

    def connection(self) -> _Context:
        return _Context(self.connection_value)


def _resolved_row() -> tuple[object, ...]:
    now = datetime.now(UTC)
    return (
        "company-1",
        "auth-subject-1",
        "principal-owner-1",
        "agent",
        now - timedelta(minutes=1),
        "instance-owner-1",
        "definition-owner",
        3,
        "cell_owner",
        "cell-1",
        "run-owner-1",
        now + timedelta(minutes=10),
    )


def _inspected_row(
    *,
    run_status: str = "running",
    instance_status: str = "active",
    expires_at: datetime | None = None,
) -> tuple[object, ...]:
    live = _resolved_row()
    expiry = live[11] if expires_at is None else expires_at
    return (*live[:11], expiry, run_status, instance_status)


@pytest.mark.asyncio
async def test_resolver_passes_only_verified_identity_inputs_to_database_function() -> None:
    resolved_row = _resolved_row()
    connection = _Connection(safe_role=True, rows=[resolved_row])
    repository = PostgresSubjectIdentityRepository(pool=_Pool(connection))  # type: ignore[arg-type]

    binding = await repository.resolve(
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
        resource_server_url=RESOURCE,
        session_id=SESSION_ID,
    )

    assert binding is not None
    assert binding.key == (ISSUER, SUBJECT, CLIENT_ID)
    assert binding.agent_instance_id == "instance-owner-1"
    assert binding.definition_id == "definition-owner"
    assert binding.definition_version == 3
    assert binding.assigned_role is Role.CELL_OWNER
    assert binding.cell_id == "cell-1"
    assert binding.principal.principal_id == "principal-owner-1"
    assert binding.auth_subject_id == "auth-subject-1"
    assert binding.principal.auth_subject_id == "auth-subject-1"
    assert binding.principal.agent_run_id == "run-owner-1"
    assert binding.agent_run_id == "run-owner-1"
    assert binding.principal.agent_identity == AgentIdentityContext(
        agent_instance_id="instance-owner-1",
        definition_id="definition-owner",
        definition_version=3,
        role=Role.CELL_OWNER,
        cell_id="cell-1",
    )
    assert binding.principal.disabled is False
    assert binding.principal.grants[0].scope == ResourceScope.company("company-1")
    assert binding.token_valid_after == resolved_row[4]
    assert binding.run_expires_at == resolved_row[11]
    assert binding.run_status == "running"
    assert binding.instance_status == "active"
    assert binding.principal.agent_run_expires_at == resolved_row[11]
    resolver_calls = [call for call in connection.calls if "anc_resolve_agent_identity" in call[0]]
    assert resolver_calls == [
        (
            resolver_calls[0][0],
            (ISSUER, SUBJECT, CLIENT_ID, RESOURCE, SESSION_ID),
        )
    ]
    assert all("anc_inspect_agent_run_binding" not in query for query, _ in connection.calls)


@pytest.mark.asyncio
async def test_unknown_or_inactive_identity_resolves_to_none() -> None:
    connection = _Connection(safe_role=True, rows=[])
    repository = PostgresSubjectIdentityRepository(  # type: ignore[arg-type]
        pool=_Pool(connection)
    )

    binding = await repository.resolve(
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
        resource_server_url=RESOURCE,
        session_id=SESSION_ID,
    )

    assert binding is None
    assert any("anc_inspect_agent_run_binding" in query for query, _ in connection.calls)


@pytest.mark.asyncio
async def test_empty_resolver_uses_inspect_for_expired_caller_run() -> None:
    now = datetime.now(UTC)
    inspect_row = _inspected_row(expires_at=now - timedelta(minutes=1))
    connection = _Connection(safe_role=True, rows=[], inspect_rows=[inspect_row])
    repository = PostgresSubjectIdentityRepository(pool=_Pool(connection))  # type: ignore[arg-type]

    binding = await repository.resolve(
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
        resource_server_url=RESOURCE,
        session_id=SESSION_ID,
    )

    assert binding is not None
    assert binding.agent_run_id == "run-owner-1"
    assert binding.run_status == "running"
    assert binding.instance_status == "active"
    assert binding.run_expires_at == inspect_row[11]
    assert any("anc_inspect_agent_run_binding" in query for query, _ in connection.calls)


@pytest.mark.asyncio
async def test_ambiguous_database_result_fails_closed() -> None:
    row = _resolved_row()
    repository = PostgresSubjectIdentityRepository(  # type: ignore[arg-type]
        pool=_Pool(_Connection(safe_role=True, rows=[row, row]))
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        await repository.resolve(
            issuer=ISSUER,
            subject=SUBJECT,
            client_id=CLIENT_ID,
            resource_server_url=RESOURCE,
            session_id=SESSION_ID,
        )


@pytest.mark.asyncio
async def test_privileged_runtime_database_role_is_rejected_before_resolution() -> None:
    connection = _Connection(safe_role=False, rows=[_resolved_row()])
    repository = PostgresSubjectIdentityRepository(pool=_Pool(connection))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="NOSUPERUSER and NOBYPASSRLS"):
        await repository.resolve(
            issuer=ISSUER,
            subject=SUBJECT,
            client_id=CLIENT_ID,
            resource_server_url=RESOURCE,
            session_id=SESSION_ID,
        )

    assert all("anc_resolve_agent_identity" not in query for query, _ in connection.calls)


def test_expired_or_revoked_resolver_row_still_builds_the_caller_binding() -> None:
    now = datetime.now(UTC)
    expired = _inspected_row(expires_at=now - timedelta(minutes=1))
    binding = PostgresSubjectIdentityRepository._binding_from_row(
        expired,
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
    )
    assert binding is not None
    assert binding.run_status == "running"
    assert binding.run_expires_at is not None
    assert binding.run_expires_at <= now

    live_binding = PostgresSubjectIdentityRepository._binding_from_row(
        _resolved_row(),
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
    )
    assert live_binding is not None
    assert live_binding.run_status == "running"
    assert live_binding.instance_status == "active"

    revoked_binding = PostgresSubjectIdentityRepository._binding_from_row(
        _inspected_row(run_status="revoked"),
        issuer=ISSUER,
        subject=SUBJECT,
        client_id=CLIENT_ID,
    )
    assert revoked_binding is not None
    assert revoked_binding.run_status == "revoked"


@pytest.mark.asyncio
async def test_expiring_run_warning_is_content_minimized() -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=10)
    connection = _Connection(safe_role=True, rows=[("run-soon", "instance-1", expires_at)])
    runs = await fetch_expiring_agent_runs(connection)
    assert runs == (
        ExpiringAgentRun(
            agent_run_id="run-soon",
            agent_instance_id="instance-1",
            expires_at=expires_at,
        ),
    )
    warning = format_expiring_agent_run_warning(runs)
    assert warning is not None
    assert "run-soon" in warning
    assert "instance-1" in warning
    assert "subject" not in warning
    assert format_expiring_agent_run_warning(()) is None

"""PostgreSQL adapter contracts for durable MCP tool-call attribution."""

from __future__ import annotations

import os
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import pytest

import ai_nature_company.postgres_attribution as postgres_attribution
from ai_nature_company.attribution import ToolCallAuditEvent, ToolCallPhase, canonical_sha256
from ai_nature_company.auth import (
    AgentIdentityContext,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.postgres_attribution import PostgresToolCallAuditSink


class _Context(AbstractAsyncContextManager[Any]):
    def __init__(self, value: object, *, connection: _Connection | None = None) -> None:
        self.value = value
        self.connection = connection

    async def __aenter__(self) -> object:
        if self.connection is not None:
            self.connection.transactions_started += 1
        return self.value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if self.connection is not None:
            if exc_type is None:
                self.connection.transactions_committed += 1
            else:
                self.connection.transactions_rolled_back += 1


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class _Connection:
    def __init__(
        self,
        *,
        safe_role: bool = True,
        fail_insert: bool = False,
        scope_company: str | None = "company-1",
        insert_returns_row: bool = True,
    ) -> None:
        self.safe_role = safe_role
        self.fail_insert = fail_insert
        self.scope_company = scope_company
        self.insert_returns_row = insert_returns_row
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.inserted: list[tuple[object, ...]] = []
        self.received: dict[tuple[object, object], datetime] = {}
        self.transactions_started = 0
        self.transactions_committed = 0
        self.transactions_rolled_back = 0

    def transaction(self) -> _Context:
        return _Context(self, connection=self)

    async def execute(
        self,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor:
        self.calls.append((query, parameters))
        if "pg_catalog.pg_roles" in query:
            return _Cursor(
                (self.scope_company, self.safe_role) if self.scope_company is not None else None
            )
        if "SELECT received_at" in query:
            received_at = self.received.get((parameters[0], parameters[1]))
            return _Cursor((received_at,) if received_at is not None else None)
        assert "INSERT INTO public.anc_mcp_tool_calls" in query
        if self.fail_insert:
            raise RuntimeError("database unavailable")
        self.inserted.append(parameters)
        if parameters[2] == ToolCallPhase.RECEIVED.value:
            self.received[(parameters[0], parameters[1])] = parameters[28]  # type: ignore[assignment]
        return _Cursor((len(self.inserted),) if self.insert_returns_row else None)


class _Pool:
    def __init__(self, connection: _Connection, *, closed: bool = False) -> None:
        self.connection_value = connection
        self.closed = closed
        self.open_calls = 0
        self.close_calls = 0

    def connection(self) -> _Context:
        return _Context(self.connection_value)

    async def open(self) -> None:
        self.open_calls += 1
        self.closed = False

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _received_event(*, occurred_at: datetime | None = None) -> ToolCallAuditEvent:
    principal = LocalTestAuthenticator(
        {
            "opaque-test-token": PrincipalRecord(
                principal_id="principal-specialist-1",
                grants=(
                    ScopedRoleGrant(
                        Role.SPECIALIST,
                        ResourceScope.company("company-1"),
                    ),
                ),
                agent_run_id="run-specialist-1",
                agent_identity=AgentIdentityContext(
                    agent_instance_id="instance-specialist-1",
                    definition_id="specialist",
                    definition_version=4,
                    role=Role.SPECIALIST,
                    cell_id="cell-1",
                ),
            )
        }
    ).authenticate("opaque-test-token")
    return ToolCallAuditEvent.received(
        call_id="call-1",
        request_id="request-1",
        protocol_version="2025-11-25",
        tool_name="experiment.propose",
        principal=principal,
        payload={
            "correlation_id": "correlation-1",
            "idempotency_key": "private-idempotency-key",
            "proposal": "private-proposal-content",
        },
        occurred_at=occurred_at or datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_received_and_success_are_committed_as_two_append_only_transactions() -> None:
    connection = _Connection()
    pool = _Pool(connection)
    sink = PostgresToolCallAuditSink(pool=pool)  # type: ignore[arg-type]
    received = _received_event()
    terminal = received.terminal(
        ToolCallPhase.SUCCEEDED,
        occurred_at=received.occurred_at + timedelta(milliseconds=47),
        duration_ms=47,
        response={"accepted": True},
    )

    await sink.record(received)

    assert connection.transactions_committed == 1
    assert len(connection.inserted) == 1
    assert received.auth_subject_id is None
    received_row = connection.inserted[0]
    assert received_row == (
        "company-1",
        "call-1",
        "received",
        "request-1",
        "correlation-1",
        "principal-specialist-1",
        None,
        "local-test",
        received.authentication_id,
        "instance-specialist-1",
        "run-specialist-1",
        "specialist",
        4,
        "specialist",
        "cell-1",
        "experiment.propose",
        "streamable_http",
        "2025-11-25",
        received.arguments_sha256,
        canonical_sha256("private-idempotency-key"),
        "pending",
        None,
        "pending",
        None,
        None,
        None,
        None,
        None,
        received.occurred_at,
        received.occurred_at,
        None,
    )

    await sink.record(terminal)

    assert connection.transactions_committed == 2
    terminal_row = connection.inserted[1]
    assert terminal_row[:2] == received_row[:2]
    assert terminal_row[2] == "succeeded"
    assert terminal_row[3:20] == received_row[3:20]
    assert terminal_row[20:] == (
        "allowed",
        "authorization.allowed",
        "succeeded",
        None,
        canonical_sha256({"accepted": True}),
        None,
        None,
        None,
        received.occurred_at,
        terminal.occurred_at,
        47,
    )
    assert "private-proposal-content" not in repr(connection.calls)
    assert "private-idempotency-key" not in repr(connection.calls)
    assert "opaque-test-token" not in repr(connection.calls)


@pytest.mark.asyncio
async def test_trusted_supabase_auth_subject_id_is_preserved_on_both_events() -> None:
    connection = _Connection()
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]
    received = replace(_received_event(), auth_subject_id="auth-subject-specialist-1")

    await sink.record(received)
    await sink.record(
        received.terminal(
            ToolCallPhase.SUCCEEDED,
            occurred_at=received.occurred_at,
            duration_ms=0,
            response={},
        )
    )

    assert [row[6] for row in connection.inserted] == [
        "auth-subject-specialist-1",
        "auth-subject-specialist-1",
    ]


@pytest.mark.parametrize(
    ("phase", "error_code", "expected_outcomes"),
    [
        (
            ToolCallPhase.DENIED,
            "forbidden",
            ("denied", "forbidden", "not_executed", "forbidden"),
        ),
        (
            ToolCallPhase.FAILED,
            "internal_error",
            ("allowed", "authorization.allowed", "failed", "internal_error"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_terminal_failure_phases_map_authorization_and_execution_outcomes(
    phase: ToolCallPhase,
    error_code: str,
    expected_outcomes: tuple[str, str, str, str],
) -> None:
    connection = _Connection()
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]
    received = _received_event()
    await sink.record(received)

    await sink.record(
        received.terminal(
            phase,
            occurred_at=received.occurred_at + timedelta(milliseconds=5),
            duration_ms=5,
            error_code=error_code,
        )
    )

    terminal_row = connection.inserted[-1]
    assert (terminal_row[20], terminal_row[21], terminal_row[22], terminal_row[23]) == (
        expected_outcomes
    )
    assert terminal_row[28] == received.occurred_at


@pytest.mark.asyncio
async def test_terminal_event_without_durable_received_event_fails_and_rolls_back() -> None:
    connection = _Connection()
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]
    received = _received_event()
    terminal = received.terminal(
        ToolCallPhase.SUCCEEDED,
        occurred_at=received.occurred_at + timedelta(milliseconds=1),
        duration_ms=1,
        response={},
    )

    with pytest.raises(RuntimeError, match="durable received"):
        await sink.record(terminal)

    assert connection.inserted == []
    assert connection.transactions_committed == 0
    assert connection.transactions_rolled_back == 1


@pytest.mark.asyncio
async def test_unsafe_database_role_is_rejected_before_insert() -> None:
    connection = _Connection(safe_role=False)
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="NOSUPERUSER and NOBYPASSRLS"):
        await sink.record(_received_event())

    assert connection.inserted == []
    assert connection.transactions_rolled_back == 1


@pytest.mark.asyncio
async def test_database_failure_propagates_and_transaction_does_not_commit() -> None:
    connection = _Connection(fail_insert=True)
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="database unavailable"):
        await sink.record(_received_event())

    assert connection.transactions_committed == 0
    assert connection.transactions_rolled_back == 1


@pytest.mark.parametrize("scope_company", [None, "another-company"])
@pytest.mark.asyncio
async def test_company_scope_must_be_established_before_insert(
    scope_company: str | None,
) -> None:
    connection = _Connection(scope_company=scope_company)
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="company scope"):
        await sink.record(_received_event())

    assert connection.inserted == []
    assert connection.transactions_rolled_back == 1


@pytest.mark.asyncio
async def test_missing_insert_result_and_invalid_received_timestamp_fail_closed() -> None:
    connection = _Connection(insert_returns_row=False)
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="insert returned no row"):
        await sink.record(_received_event())

    received = _received_event()
    connection = _Connection()
    connection.received[(received.company_id, received.call_id)] = datetime(2026, 8, 30)
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]
    terminal = received.terminal(
        ToolCallPhase.SUCCEEDED,
        occurred_at=received.occurred_at + timedelta(milliseconds=1),
        duration_ms=1,
        response={},
    )
    with pytest.raises(RuntimeError, match="invalid timestamp"):
        await sink.record(terminal)


@pytest.mark.asyncio
async def test_supplied_pool_is_opened_but_not_closed_by_sink() -> None:
    pool = _Pool(_Connection(), closed=True)
    sink = PostgresToolCallAuditSink(
        pool=pool,  # type: ignore[arg-type]
        transport="stdio",
    )

    await sink.record(_received_event())
    await sink.close()

    assert pool.open_calls == 1
    assert pool.close_calls == 0
    assert pool.closed is False
    assert pool.connection_value.inserted[0][16] == "stdio"


@pytest.mark.asyncio
async def test_dsn_created_pool_is_owned_and_context_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool(_Connection(), closed=True)

    def pool_factory(**kwargs: object) -> _Pool:
        assert kwargs == {
            "conninfo": "postgresql://runtime@example.test/database",
            "min_size": 2,
            "max_size": 3,
            "open": False,
        }
        return pool

    monkeypatch.setattr(postgres_attribution, "PsycopgAsyncConnectionPool", pool_factory)
    async with PostgresToolCallAuditSink(
        "postgresql://runtime@example.test/database",
        min_size=2,
        max_size=3,
    ) as sink:
        await sink.record(_received_event())

    assert pool.open_calls == 1
    assert pool.close_calls == 1
    assert pool.closed is True


@pytest.mark.asyncio
async def test_invalid_transport_and_inconsistent_events_fail_before_database_access() -> None:
    pool = _Pool(_Connection())
    with pytest.raises(ValueError, match="transport"):
        PostgresToolCallAuditSink(pool=pool, transport=" streamable_http ")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="64"):
        PostgresToolCallAuditSink(pool=pool, transport="x" * 65)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="dsn"):
        PostgresToolCallAuditSink()

    received = _received_event()
    sink = PostgresToolCallAuditSink(pool=pool)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="received events"):
        await sink.record(replace(received, duration_ms=1))
    with pytest.raises(TypeError, match="ToolCallAuditEvent"):
        await sink.record(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ToolCallPhase"):
        await sink.record(replace(received, phase="received"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        await sink.record(replace(received, occurred_at=datetime(2026, 8, 30)))

    succeeded = received.terminal(
        ToolCallPhase.SUCCEEDED,
        occurred_at=received.occurred_at,
        duration_ms=0,
        response={},
    )
    with pytest.raises(ValueError, match="duration_ms"):
        await sink.record(replace(succeeded, duration_ms=None))
    with pytest.raises(ValueError, match="cannot contain an error_code"):
        await sink.record(replace(succeeded, error_code="unexpected"))
    with pytest.raises(ValueError, match="failed events require"):
        await sink.record(replace(succeeded, phase=ToolCallPhase.FAILED, error_code=None))

    assert pool.connection_value.calls == []


@pytest.mark.asyncio
async def test_denial_without_specific_code_uses_bounded_fallback_reason() -> None:
    connection = _Connection()
    sink = PostgresToolCallAuditSink(pool=_Pool(connection))  # type: ignore[arg-type]
    received = _received_event()
    await sink.record(received)

    await sink.record(
        received.terminal(
            ToolCallPhase.DENIED,
            occurred_at=received.occurred_at,
            duration_ms=0,
        )
    )

    assert connection.inserted[-1][21] == "authorization.denied"


@pytest.mark.skipif(
    not os.getenv("ANC_TEST_DATABASE_URL"),
    reason="set ANC_TEST_DATABASE_URL to run real PostgreSQL attribution tests",
)
@pytest.mark.asyncio
async def test_real_postgres_received_and_terminal_events_are_append_only() -> None:
    psycopg = pytest.importorskip("psycopg")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    database_url = os.environ["ANC_TEST_DATABASE_URL"]
    migrations = [path.read_text() for path in sorted(Path("supabase/migrations").glob("*.sql"))]
    suffix = uuid4().hex
    company_id = f"audit-company-{suffix}"
    client_id = f"audit-client-{suffix}"
    auth_subject_id = f"audit-subject-{suffix}"
    principal_id = f"audit-principal-{suffix}"
    instance_id = f"audit-instance-{suffix}"
    run_id = f"audit-run-{suffix}"
    now = datetime.now(UTC)

    async with await psycopg.AsyncConnection.connect(
        database_url,
        autocommit=True,
    ) as connection:
        for migration in migrations:
            await connection.execute(migration)
        await connection.execute(
            """
            INSERT INTO public.anc_oauth_clients
                (client_id, company_id, resource_url, display_name, status,
                 revision, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'active', 0, %s, %s)
            """,
            (
                client_id,
                company_id,
                "https://substrate.example.test/mcp",
                "Attribution integration client",
                now,
                now,
            ),
        )
        await connection.execute(
            """
            INSERT INTO public.anc_auth_subjects
                (company_id, auth_subject_id, issuer, external_subject,
                 oauth_client_id, principal_id, subject_kind, status,
                 token_valid_after, disabled_at, revision, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, 'agent', 'active',
                 %s, NULL, 0, %s, %s)
            """,
            (
                company_id,
                auth_subject_id,
                "https://project-ref.supabase.co/auth/v1",
                str(uuid4()),
                client_id,
                principal_id,
                now,
                now,
                now,
            ),
        )
        await connection.execute(
            """
            INSERT INTO public.anc_agent_instances
                (company_id, agent_instance_id, principal_id, definition_id,
                 definition_version, role, cell_id, status, revision,
                 created_at, updated_at)
            VALUES
                (%s, %s, %s, 'specialist', 4, 'specialist', 'cell-1',
                 'active', 0, %s, %s)
            """,
            (company_id, instance_id, principal_id, now, now),
        )
        await connection.execute(
            """
            INSERT INTO public.anc_agent_runs
                (company_id, agent_run_id, agent_instance_id, principal_id,
                 definition_id, definition_version, role, cell_id, status,
                 termination_code, revision, started_at, expires_at, ended_at,
                 created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, 'specialist', 4, 'specialist', 'cell-1',
                 'running', NULL, 0, %s, %s, NULL, %s, %s)
            """,
            (
                company_id,
                run_id,
                instance_id,
                principal_id,
                now,
                now + timedelta(hours=1),
                now,
                now,
            ),
        )

    async def assume_runtime_role(connection: object) -> None:
        await connection.execute("SET ROLE ai_nature_company_runtime")
        await connection.execute("SET row_security = on")
        await connection.execute("SELECT set_config('app.company_id', '', false)")
        await connection.commit()

    pool = psycopg_pool.AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=1,
        open=False,
        configure=assume_runtime_role,
    )
    received = replace(
        _received_event(occurred_at=now),
        company_id=company_id,
        principal_id=principal_id,
        auth_subject_id=auth_subject_id,
        agent_instance_id=instance_id,
        agent_run_id=run_id,
    )
    terminal = received.terminal(
        ToolCallPhase.SUCCEEDED,
        occurred_at=now + timedelta(milliseconds=12),
        duration_ms=12,
        response={"accepted": True},
    )
    async with pool:
        sink = PostgresToolCallAuditSink(pool=pool)
        await sink.record(received)
        await sink.record(terminal)

    async with await psycopg.AsyncConnection.connect(
        database_url,
        autocommit=True,
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT phase, authorization_outcome, authorization_reason_code,
                   execution_outcome, received_at, recorded_at, duration_ms,
                   arguments_sha256, response_sha256, auth_subject_id
            FROM public.anc_mcp_tool_calls
            WHERE company_id = %s AND call_id = %s
            ORDER BY position
            """,
            (company_id, received.call_id),
        )
        rows = await cursor.fetchall()

    assert [row[0] for row in rows] == ["received", "succeeded"]
    assert rows[0][1:4] == ("pending", None, "pending")
    assert rows[1][1:4] == ("allowed", "authorization.allowed", "succeeded")
    assert rows[1][4] == rows[0][4] == now
    assert rows[1][5] == terminal.occurred_at
    assert rows[1][6] == 12
    assert rows[0][7] == received.arguments_sha256
    assert rows[1][8] == terminal.response_sha256
    assert [row[9] for row in rows] == [auth_subject_id, auth_subject_id]

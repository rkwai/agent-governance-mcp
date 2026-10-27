"""Async psycopg repository for the Phase 1 persistence contract.

Migrations are intentionally managed outside this module (the canonical SQL lives in
``supabase/migrations``).  This adapter always starts a database transaction and sets
``app.company_id`` locally before issuing application queries, allowing PostgreSQL RLS
to fail closed even if a future query accidentally omits a company predicate.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from .errors import BudgetError, ConflictError, LeaseError, NotFoundError, ValidationError
from .ledger_units import identity_of, ledger_label, normalize_ledger_identity
from .store import (
    Record,
    RecordMapping,
    StoreTransaction,
    TransactionalRepository,
    _budget_group_record,
    _money,
    _page,
    _positive_money,
    _redact_export_secrets,
    _required,
    _reservation_applied_amount,
    _reservation_commit_delta,
    _reservation_hold_amount,
    _reservation_member_id,
    _reservation_remaining_hold,
    _revision,
    _ttl,
    _utc,
)

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

try:
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg_pool import AsyncConnectionPool as PsycopgAsyncConnectionPool
except ImportError:  # pragma: no cover - exercised only in dependency-broken installs
    dict_row = None
    Jsonb = None
    PsycopgAsyncConnectionPool = None


def _jsonable(value: Any) -> Any:
    """Convert common domain values to deterministic JSON-compatible values."""

    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, (UUID, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValidationError(f"{type(value).__name__} is not JSON serializable")


def _json_copy(value: RecordMapping) -> Record:
    # A round-trip also ensures no caller-owned nested object can mutate persisted state.
    return json.loads(json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")))


def _jsonb(value: RecordMapping) -> Any:
    if Jsonb is None:  # pragma: no cover - guarded by repository construction
        raise RuntimeError("psycopg JSON support is unavailable")
    return Jsonb(_json_copy(value))


class PostgresRepository(TransactionalRepository):
    """Real PostgreSQL repository with an async connection pool.

    Pass either a DSN or an already configured ``AsyncConnectionPool``.  A pool made
    by this class is opened lazily on the first transaction and should be closed with
    ``await repository.close()`` or the async context-manager form.
    """

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
                    "PostgresRepository requires the project dependency psycopg[binary,pool]"
                )
            if not dsn:
                raise ValidationError("dsn is required when pool is not supplied")
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

    async def __aenter__(self) -> PostgresRepository:
        await self.open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def open(self) -> None:
        if getattr(self._pool, "closed", False):
            await self._pool.open()

    async def close(self) -> None:
        if self._owns_pool and not getattr(self._pool, "closed", True):
            await self._pool.close()

    @asynccontextmanager
    async def transaction(self, company_id: str) -> AsyncIterator[StoreTransaction]:
        company_id = _required(company_id, "company_id")
        await self.open()
        async with self._pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                SELECT set_config('app.company_id', %s, true),
                       NOT (role.rolsuper OR role.rolbypassrls) AS rls_enforced
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = current_user
                """,
                (company_id,),
            )
            row = await cursor.fetchone()
            if row is None or row[0] != company_id:
                raise RuntimeError("database company scope could not be established")
            if row[1] is not True:
                raise RuntimeError("database runtime role must be NOSUPERUSER and NOBYPASSRLS")
            yield _PostgresTransaction(connection, company_id)


class _PostgresTransaction:
    def __init__(self, connection: AsyncConnection[Any], company_id: str) -> None:
        self._connection = connection
        self._company_id = company_id

    @property
    def company_id(self) -> str:
        return self._company_id

    async def _fetchone(self, query: str, parameters: tuple[Any, ...] = ()) -> Record | None:
        if dict_row is None:  # pragma: no cover - guarded by repository construction
            raise RuntimeError("psycopg row support is unavailable")
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, parameters)
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def _fetchall(self, query: str, parameters: tuple[Any, ...] = ()) -> list[Record]:
        if dict_row is None:  # pragma: no cover - guarded by repository construction
            raise RuntimeError("psycopg row support is unavailable")
        async with self._connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, parameters)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _execute(self, query: str, parameters: tuple[Any, ...] = ()) -> int:
        cursor = await self._connection.execute(query, parameters)
        return cursor.rowcount

    async def append_event(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        actor_id: str,
        payload: RecordMapping,
        expected_revision: int | None = None,
        event_id: str | None = None,
        cell_id: str | None = None,
        schema_version: int = 1,
        occurred_at: datetime | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        metadata: RecordMapping | None = None,
    ) -> Record:
        aggregate_type = _required(aggregate_type, "aggregate_type")
        aggregate_id = _required(aggregate_id, "aggregate_id")
        event_type = _required(event_type, "event_type")
        actor_id = _required(actor_id, "actor_id")
        expected_revision = _revision(expected_revision)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version <= 0
        ):
            raise ValidationError("schema_version must be greater than zero")
        event_id = _required(event_id or str(uuid4()), "event_id")
        idempotency_key = (
            _required(idempotency_key, "idempotency_key") if idempotency_key is not None else None
        )
        correlation_id = str(correlation_id) if correlation_id is not None else None
        causation_id = str(causation_id) if causation_id is not None else None
        payload_copy = _json_copy(payload)
        metadata_copy = _json_copy(metadata or {})
        existing = await self._fetchone(
            """
            SELECT event_id, company_id, aggregate_type, aggregate_id, event_type,
                   aggregate_revision, actor_id, cell_id, schema_version, occurred_at,
                   payload, metadata,
                   idempotency_key, correlation_id, causation_id, position
            FROM public.anc_domain_events
            WHERE company_id = %s AND event_id = %s
            """,
            (self.company_id, event_id),
        )
        immutable = {
            "company_id": self.company_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "cell_id": str(cell_id) if cell_id is not None else None,
            "schema_version": schema_version,
            "payload": payload_copy,
            "metadata": metadata_copy,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
        }
        if existing is not None:
            if all(existing[field] == value for field, value in immutable.items()):
                return existing
            raise ConflictError(f"event_id {event_id!r} already identifies another event")

        await self._execute(
            """
            INSERT INTO public.anc_stream_heads
                (company_id, aggregate_type, aggregate_id, revision)
            VALUES (%s, %s, %s, 0)
            ON CONFLICT (company_id, aggregate_type, aggregate_id) DO NOTHING
            """,
            (self.company_id, aggregate_type, aggregate_id),
        )
        head = await self._fetchone(
            """
            SELECT revision
            FROM public.anc_stream_heads
            WHERE company_id = %s AND aggregate_type = %s AND aggregate_id = %s
            FOR UPDATE
            """,
            (self.company_id, aggregate_type, aggregate_id),
        )
        if head is None:
            raise RuntimeError("stream head was not visible inside its company scope")
        # A concurrent retry may have inserted the same event while this transaction
        # waited for the stream-head lock. Recheck before treating its old expected
        # revision as stale.
        existing = await self._fetchone(
            """
            SELECT event_id, company_id, aggregate_type, aggregate_id, event_type,
                   aggregate_revision, actor_id, cell_id, schema_version, occurred_at,
                   payload, metadata,
                   idempotency_key, correlation_id, causation_id, position
            FROM public.anc_domain_events
            WHERE company_id = %s AND event_id = %s
            """,
            (self.company_id, event_id),
        )
        if existing is not None:
            if all(existing[field] == value for field, value in immutable.items()):
                return existing
            raise ConflictError(f"event_id {event_id!r} already identifies another event")
        current_revision = head["revision"]
        if expected_revision is not None and current_revision != expected_revision:
            raise ConflictError(
                "stale aggregate revision: "
                f"expected {expected_revision}, current {current_revision}"
            )
        revision = current_revision + 1
        await self._execute(
            """
            UPDATE public.anc_stream_heads
            SET revision = %s
            WHERE company_id = %s AND aggregate_type = %s AND aggregate_id = %s
            """,
            (revision, self.company_id, aggregate_type, aggregate_id),
        )
        record = await self._fetchone(
            """
            INSERT INTO public.anc_domain_events
                (company_id, event_id, aggregate_type, aggregate_id, event_type,
                 aggregate_revision, actor_id, cell_id, schema_version, occurred_at,
                 payload, metadata, idempotency_key, correlation_id, causation_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING event_id, company_id, aggregate_type, aggregate_id, event_type,
                      aggregate_revision, actor_id, cell_id, schema_version, occurred_at,
                      payload, metadata,
                      idempotency_key, correlation_id, causation_id, position
            """,
            (
                self.company_id,
                event_id,
                aggregate_type,
                aggregate_id,
                event_type,
                revision,
                actor_id,
                str(cell_id) if cell_id is not None else None,
                schema_version,
                _utc(occurred_at),
                _jsonb(payload_copy),
                _jsonb(metadata_copy),
                idempotency_key,
                correlation_id,
                causation_id,
            ),
        )
        if record is None:
            raise RuntimeError("event insert returned no row")
        return record

    async def append_domain_event(self, **fields: Any) -> Record:
        return await self.append_event(**fields)

    async def append_audit_event(
        self,
        *,
        action: str,
        outcome: str,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        details: RecordMapping | None = None,
        audit_id: str | None = None,
        occurred_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> Record:
        audit_id = _required(audit_id or str(uuid4()), "audit_id")
        correlation_id = str(correlation_id) if correlation_id is not None else None
        details_copy = _json_copy(details or {})
        immutable = {
            "company_id": self.company_id,
            "action": _required(action, "action"),
            "outcome": _required(outcome, "outcome"),
            "actor_id": _required(actor_id, "actor_id"),
            "resource_type": _required(resource_type, "resource_type"),
            "resource_id": _required(resource_id, "resource_id"),
            "details": details_copy,
            "correlation_id": correlation_id,
        }
        record = await self._fetchone(
            """
            INSERT INTO public.anc_audit_events
                (company_id, audit_id, action, outcome, actor_id, resource_type,
                 resource_id, occurred_at, details, correlation_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (company_id, audit_id) DO NOTHING
            RETURNING audit_id, company_id, action, outcome, actor_id, resource_type,
                      resource_id, occurred_at, details, correlation_id, position
            """,
            (
                self.company_id,
                audit_id,
                immutable["action"],
                immutable["outcome"],
                immutable["actor_id"],
                immutable["resource_type"],
                immutable["resource_id"],
                _utc(occurred_at),
                _jsonb(details_copy),
                correlation_id,
            ),
        )
        if record is None:
            record = await self._fetchone(
                """
                SELECT audit_id, company_id, action, outcome, actor_id, resource_type,
                       resource_id, occurred_at, details, correlation_id, position
                FROM public.anc_audit_events
                WHERE company_id = %s AND audit_id = %s
                """,
                (self.company_id, audit_id),
            )
        if record is None:
            raise RuntimeError("audit event was not visible after insert conflict")
        if not all(record[field] == value for field, value in immutable.items()):
            raise ConflictError(f"audit_id {audit_id!r} already identifies another event")
        return record

    async def get_projection(self, projection_type: str, projection_id: str) -> Record | None:
        return await self._fetchone(
            """
            SELECT company_id, projection_type, projection_id, revision, state,
                   created_at, updated_at
            FROM public.anc_projections
            WHERE company_id = %s AND projection_type = %s AND projection_id = %s
            """,
            (
                self.company_id,
                _required(projection_type, "projection_type"),
                _required(projection_id, "projection_id"),
            ),
        )

    async def put_projection(
        self,
        *,
        projection_type: str,
        projection_id: str,
        state: RecordMapping,
        expected_revision: int,
        updated_at: datetime | None = None,
    ) -> Record:
        expected_revision = _revision(expected_revision, required=True)
        assert expected_revision is not None
        projection_type = _required(projection_type, "projection_type")
        projection_id = _required(projection_id, "projection_id")
        timestamp = _utc(updated_at)
        state_json = _jsonb(state)
        if expected_revision == 0:
            record = await self._fetchone(
                """
                INSERT INTO public.anc_projections
                    (company_id, projection_type, projection_id, revision, state,
                     created_at, updated_at)
                VALUES (%s, %s, %s, 1, %s, %s, %s)
                ON CONFLICT (company_id, projection_type, projection_id) DO NOTHING
                RETURNING company_id, projection_type, projection_id, revision, state,
                          created_at, updated_at
                """,
                (
                    self.company_id,
                    projection_type,
                    projection_id,
                    state_json,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            record = await self._fetchone(
                """
                UPDATE public.anc_projections
                SET revision = revision + 1, state = %s, updated_at = %s
                WHERE company_id = %s AND projection_type = %s AND projection_id = %s
                  AND revision = %s
                RETURNING company_id, projection_type, projection_id, revision, state,
                          created_at, updated_at
                """,
                (
                    state_json,
                    timestamp,
                    self.company_id,
                    projection_type,
                    projection_id,
                    expected_revision,
                ),
            )
        if record is None:
            current = await self.get_projection(projection_type, projection_id)
            revision = current["revision"] if current is not None else 0
            raise ConflictError(
                f"stale projection revision: expected {expected_revision}, current {revision}"
            )
        return record

    async def list_projections(
        self,
        *,
        projection_type: str | None = None,
        after_projection_id: str | None = None,
        cell_id: str | None = None,
        limit: int = 1_000,
    ) -> list[Record]:
        limit = _page(limit)
        if projection_type is not None:
            projection_type = _required(projection_type, "projection_type")
        return await self._fetchall(
            """
            SELECT company_id, projection_type, projection_id, revision, state,
                   created_at, updated_at
            FROM public.anc_projections
            WHERE company_id = %s
              AND (%s::text IS NULL OR projection_type = %s)
              AND (%s::text IS NULL OR projection_id > %s)
              AND (%s::text IS NULL OR state->>'cell_id' = %s)
            ORDER BY projection_type, projection_id
            LIMIT %s
            """,
            (
                self.company_id,
                projection_type,
                projection_type,
                after_projection_id,
                after_projection_id,
                cell_id,
                cell_id,
                limit,
            ),
        )

    async def claim_idempotency(
        self,
        *,
        namespace: str,
        key: str,
        request_hash: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> Record:
        namespace = _required(namespace, "namespace")
        key = _required(key, "key")
        request_hash = _required(request_hash, "request_hash")
        timestamp = _utc(now)
        expiry = _utc(expires_at) if expires_at is not None else None
        if expiry is not None and expiry <= timestamp:
            raise ValidationError("expires_at must be later than now")
        inserted = await self._fetchone(
            """
            INSERT INTO public.anc_idempotency_records
                (company_id, namespace, idempotency_key, request_hash, status,
                 response, error, revision, created_at, updated_at, expires_at)
            VALUES (%s, %s, %s, %s, 'in_progress', NULL, NULL, 1, %s, %s, %s)
            ON CONFLICT (company_id, namespace, idempotency_key) DO NOTHING
            RETURNING company_id, namespace, idempotency_key AS key, request_hash,
                      status, response, error, revision, created_at, updated_at, expires_at
            """,
            (self.company_id, namespace, key, request_hash, timestamp, timestamp, expiry),
        )
        if inserted is not None:
            return inserted
        existing = await self._fetchone(
            """
            SELECT company_id, namespace, idempotency_key AS key, request_hash,
                   status, response, error, revision, created_at, updated_at, expires_at
            FROM public.anc_idempotency_records
            WHERE company_id = %s AND namespace = %s AND idempotency_key = %s
            FOR UPDATE
            """,
            (self.company_id, namespace, key),
        )
        if existing is None:
            raise RuntimeError("idempotency record disappeared during claim")
        if existing["expires_at"] is None or existing["expires_at"] > timestamp:
            if existing["request_hash"] != request_hash:
                raise ConflictError("idempotency key was already used for a different request")
            return existing
        record = await self._fetchone(
            """
            UPDATE public.anc_idempotency_records
            SET request_hash = %s, status = 'in_progress', response = NULL, error = NULL,
                revision = revision + 1, created_at = %s, updated_at = %s, expires_at = %s
            WHERE company_id = %s AND namespace = %s AND idempotency_key = %s
            RETURNING company_id, namespace, idempotency_key AS key, request_hash,
                      status, response, error, revision, created_at, updated_at, expires_at
            """,
            (
                request_hash,
                timestamp,
                timestamp,
                expiry,
                self.company_id,
                namespace,
                key,
            ),
        )
        if record is None:
            raise RuntimeError("expired idempotency record could not be reclaimed")
        return record

    async def begin_idempotency(self, **fields: Any) -> Record:
        return await self.claim_idempotency(**fields)

    async def get_idempotency(self, namespace: str, key: str) -> Record | None:
        return await self._fetchone(
            """
            SELECT company_id, namespace, idempotency_key AS key, request_hash,
                   status, response, error, revision, created_at, updated_at, expires_at
            FROM public.anc_idempotency_records
            WHERE company_id = %s AND namespace = %s AND idempotency_key = %s
            """,
            (
                self.company_id,
                _required(namespace, "namespace"),
                _required(key, "key"),
            ),
        )

    async def complete_idempotency(
        self,
        *,
        namespace: str,
        key: str,
        request_hash: str,
        response: RecordMapping,
        now: datetime | None = None,
    ) -> Record:
        return await self._finish_idempotency(
            namespace=namespace,
            key=key,
            request_hash=request_hash,
            status="completed",
            value=response,
            now=now,
        )

    async def fail_idempotency(
        self,
        *,
        namespace: str,
        key: str,
        request_hash: str,
        error: RecordMapping,
        now: datetime | None = None,
    ) -> Record:
        return await self._finish_idempotency(
            namespace=namespace,
            key=key,
            request_hash=request_hash,
            status="failed",
            value=error,
            now=now,
        )

    async def _finish_idempotency(
        self,
        *,
        namespace: str,
        key: str,
        request_hash: str,
        status: str,
        value: RecordMapping,
        now: datetime | None,
    ) -> Record:
        namespace = _required(namespace, "namespace")
        key = _required(key, "key")
        request_hash = _required(request_hash, "request_hash")
        existing = await self._fetchone(
            """
            SELECT company_id, namespace, idempotency_key AS key, request_hash,
                   status, response, error, revision, created_at, updated_at, expires_at
            FROM public.anc_idempotency_records
            WHERE company_id = %s AND namespace = %s AND idempotency_key = %s
            FOR UPDATE
            """,
            (self.company_id, namespace, key),
        )
        if existing is None:
            raise NotFoundError("idempotency claim does not exist")
        if existing["request_hash"] != request_hash:
            raise ConflictError("idempotency key belongs to a different request")
        target_field = "response" if status == "completed" else "error"
        value_copy = _json_copy(value)
        if existing["status"] == status:
            if existing[target_field] == value_copy:
                return existing
            raise ConflictError("idempotency outcome is already immutable")
        if existing["status"] != "in_progress":
            raise ConflictError(f"idempotency claim is already {existing['status']}")
        response = _jsonb(value_copy) if status == "completed" else None
        error = _jsonb(value_copy) if status == "failed" else None
        record = await self._fetchone(
            """
            UPDATE public.anc_idempotency_records
            SET status = %s, response = %s, error = %s, revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND namespace = %s AND idempotency_key = %s
            RETURNING company_id, namespace, idempotency_key AS key, request_hash,
                      status, response, error, revision, created_at, updated_at, expires_at
            """,
            (
                status,
                response,
                error,
                _utc(now),
                self.company_id,
                namespace,
                key,
            ),
        )
        if record is None:
            raise RuntimeError("idempotency outcome update returned no row")
        return record

    async def create_budget(
        self,
        *,
        budget_id: str,
        scope: str,
        limit: Decimal | int | str,
        currency: str | None = None,
        unit: str | None = None,
        parent_budget_id: str | None = None,
        stage: str | None = None,
        cell_id: str | None = None,
        agent_id: str | None = None,
        experiment_id: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        status: str = "active",
        metadata: RecordMapping | None = None,
        now: datetime | None = None,
    ) -> Record:
        budget_id = _required(budget_id, "budget_id")
        scope = _required(scope, "scope")
        unit, currency = normalize_ledger_identity(unit, currency)
        limit_amount = _money(limit, "limit")
        if limit_amount < 0:
            raise ValidationError("limit must not be negative")
        timestamp = _utc(now)
        start = _utc(starts_at) if starts_at is not None else timestamp
        end = _utc(ends_at) if ends_at is not None else None
        if end is not None and end <= start:
            raise ValidationError("ends_at must be later than starts_at")
        parent_budget_id = (
            _required(parent_budget_id, "parent_budget_id")
            if parent_budget_id is not None
            else None
        )
        stage = _required(stage, "stage").lower() if stage is not None else None
        if scope == "stage":
            if stage not in {"exploration", "exploitation"}:
                raise ValidationError("stage budgets require stage exploration or exploitation")
        elif stage is not None:
            raise ValidationError("stage is only valid for stage budgets")
        if parent_budget_id == budget_id:
            raise ValidationError("a budget cannot be its own parent")
        if parent_budget_id is not None:
            parent = await self.get_budget(parent_budget_id)
            if parent is None:
                raise NotFoundError(f"parent budget {parent_budget_id!r} does not exist")
            if identity_of(parent) != (unit, currency):
                raise ValidationError("child budget unit of account must match its parent")
            if start < parent["starts_at"] or (
                parent["ends_at"] is not None and (end is None or end > parent["ends_at"])
            ):
                raise ValidationError("child budget window must fit inside its parent")
        status = _required(status, "status")
        metadata_copy = _json_copy(metadata or {})
        record = await self._fetchone(
            """
            INSERT INTO public.anc_budgets
                (company_id, budget_id, scope, parent_budget_id, stage,
                 limit_amount, spent_amount,
                 reserved_amount, unit, currency, cell_id, agent_id,
                 experiment_id, status, revision,
                 starts_at, ends_at, metadata, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, %s, %s, %s, %s, 0,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (company_id, budget_id) DO NOTHING
            RETURNING company_id, budget_id, scope, parent_budget_id, stage,
                      limit_amount AS limit,
                      spent_amount AS spent, reserved_amount AS reserved, unit, currency,
                      cell_id, agent_id, experiment_id, status, revision, starts_at,
                      ends_at, metadata,
                      created_at, updated_at
            """,
            (
                self.company_id,
                budget_id,
                scope,
                parent_budget_id,
                stage,
                limit_amount,
                unit,
                currency,
                str(cell_id) if cell_id is not None else None,
                str(agent_id) if agent_id is not None else None,
                str(experiment_id) if experiment_id is not None else None,
                status,
                start,
                end,
                _jsonb(metadata_copy),
                timestamp,
                timestamp,
            ),
        )
        if record is not None:
            return record
        existing = await self.get_budget(budget_id)
        if existing is None:
            raise RuntimeError("budget was not visible after insert conflict")
        immutable = {
            "scope": scope,
            "parent_budget_id": parent_budget_id,
            "stage": stage,
            "limit": limit_amount,
            "unit": unit,
            "currency": currency,
            "cell_id": str(cell_id) if cell_id is not None else None,
            "agent_id": str(agent_id) if agent_id is not None else None,
            "experiment_id": str(experiment_id) if experiment_id is not None else None,
            "status": status,
            "starts_at": start,
            "ends_at": end,
            "metadata": metadata_copy,
        }
        if all(existing[field] == value for field, value in immutable.items()):
            return existing
        raise ConflictError(f"budget_id {budget_id!r} already exists")

    async def get_budget(self, budget_id: str) -> Record | None:
        return await self._fetchone(
            """
            SELECT company_id, budget_id, scope, parent_budget_id, stage,
                   limit_amount AS limit,
                   spent_amount AS spent, reserved_amount AS reserved, unit, currency,
                   cell_id, agent_id, experiment_id, status, revision, starts_at,
                   ends_at, metadata,
                   created_at, updated_at
            FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = %s
            """,
            (self.company_id, _required(budget_id, "budget_id")),
        )

    async def list_budgets(
        self,
        *,
        scope: str | None = None,
        cell_id: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        limit: int = 1_000,
    ) -> list[Record]:
        limit = _page(limit)
        scope = _required(scope, "scope") if scope is not None else None
        cell_id = _required(cell_id, "cell_id") if cell_id is not None else None
        stage = _required(stage, "stage").lower() if stage is not None else None
        status = _required(status, "status") if status is not None else None
        return await self._fetchall(
            """
            SELECT company_id, budget_id, scope, parent_budget_id, stage,
                   limit_amount AS limit, spent_amount AS spent,
                   reserved_amount AS reserved, unit, currency, cell_id, agent_id,
                   experiment_id, status, revision, starts_at, ends_at, metadata,
                   created_at, updated_at
            FROM public.anc_budgets
            WHERE company_id = %s
              AND (%s::text IS NULL OR scope = %s)
              AND (%s::text IS NULL OR cell_id = %s)
              AND (%s::text IS NULL OR stage = %s)
              AND (%s::text IS NULL OR status = %s)
            ORDER BY budget_id
            LIMIT %s
            """,
            (
                self.company_id,
                scope,
                scope,
                cell_id,
                cell_id,
                stage,
                stage,
                status,
                status,
                limit,
            ),
        )

    async def update_budget(
        self,
        *,
        budget_id: str,
        expected_revision: int,
        status: str | None = None,
        limit: Decimal | int | str | None = None,
        metadata: RecordMapping | None = None,
        now: datetime | None = None,
    ) -> Record:
        """Change limit or status on one ledger. Unit and currency stay immutable."""

        budget_id = _required(budget_id, "budget_id")
        expected_revision = _revision(expected_revision, required=True)
        assert expected_revision is not None
        if status is None and limit is None and metadata is None:
            raise ValidationError("update_budget requires status, limit, or metadata")
        if status is not None:
            status = _required(status, "status")
            if status not in {"active", "exhausted", "closed"}:
                raise ValidationError("budget status must be active, exhausted, or closed")
        timestamp = _utc(now)
        budget = await self._fetchone(
            """
            SELECT company_id, budget_id, scope, parent_budget_id, stage,
                   limit_amount AS limit,
                   spent_amount AS spent, reserved_amount AS reserved, unit, currency,
                   cell_id, agent_id, experiment_id, status, revision, starts_at,
                   ends_at, metadata,
                   created_at, updated_at
            FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = %s
            FOR UPDATE
            """,
            (self.company_id, budget_id),
        )
        if budget is None:
            raise NotFoundError(f"budget {budget_id!r} does not exist")
        if budget["revision"] != expected_revision:
            raise ConflictError(
                f"stale budget revision: expected {expected_revision}, current {budget['revision']}"
            )
        if budget["status"] == "closed":
            raise BudgetError("budget is closed")
        next_limit = budget["limit"] if limit is None else _money(limit, "limit")
        if next_limit < 0:
            raise ValidationError("limit must not be negative")
        if budget["spent"] + budget["reserved"] > next_limit:
            raise BudgetError("new limit is below spent plus reserved")
        if status == "active" and budget["status"] != "active":
            raise ValidationError("a closed or exhausted budget cannot be reopened")
        next_metadata = budget["metadata"] if metadata is None else _json_copy(metadata)
        record = await self._fetchone(
            """
            UPDATE public.anc_budgets
            SET limit_amount = %s,
                status = %s,
                metadata = %s,
                revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND budget_id = %s AND revision = %s
            RETURNING company_id, budget_id, scope, parent_budget_id, stage,
                      limit_amount AS limit,
                      spent_amount AS spent, reserved_amount AS reserved, unit, currency,
                      cell_id, agent_id, experiment_id, status, revision, starts_at,
                      ends_at, metadata,
                      created_at, updated_at
            """,
            (
                next_limit,
                status if status is not None else budget["status"],
                _jsonb(next_metadata),
                timestamp,
                self.company_id,
                budget_id,
                expected_revision,
            ),
        )
        if record is None:
            raise ConflictError(
                f"stale budget revision: expected {expected_revision}, current {budget['revision']}"
            )
        return record

    async def reserve_budget(
        self,
        *,
        budget_id: str,
        reservation_id: str,
        amount: Decimal | int | str,
        purpose: str,
        expected_revision: int | None = None,
        expires_at: datetime | None = None,
        idempotency_key: str | None = None,
        metadata: RecordMapping | None = None,
        now: datetime | None = None,
    ) -> Record:
        budget_id = _required(budget_id, "budget_id")
        reservation_id = _required(reservation_id, "reservation_id")
        purpose = _required(purpose, "purpose")
        amount_value = _positive_money(amount)
        expected_revision = _revision(expected_revision)
        timestamp = _utc(now)
        expiry = _utc(expires_at) if expires_at is not None else None
        if expiry is not None and expiry <= timestamp:
            raise ValidationError("expires_at must be later than now")
        metadata_copy = _json_copy(metadata or {})
        # The reservation row may not exist yet, so use a transaction advisory lock
        # to serialize duplicate IDs before locking their budget rows.
        await self._execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (json.dumps([self.company_id, "reservation", reservation_id]),),
        )
        existing = await self._get_reservation(reservation_id, for_update=True)
        immutable = {
            "budget_id": budget_id,
            "reservation_group_id": reservation_id,
            "ledger_role": "budget",
            "amount": amount_value,
            "purpose": purpose,
            "idempotency_key": idempotency_key,
            "metadata": metadata_copy,
        }
        if existing is not None:
            if all(existing[field] == value for field, value in immutable.items()):
                return existing
            raise ConflictError(f"reservation_id {reservation_id!r} already exists")
        budget = await self._fetchone(
            """
            SELECT company_id, budget_id, scope, parent_budget_id, stage,
                   limit_amount AS limit,
                   spent_amount AS spent, reserved_amount AS reserved, unit, currency,
                   cell_id, agent_id, experiment_id, status, revision, starts_at,
                   ends_at, metadata,
                   created_at, updated_at
            FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = %s
            FOR UPDATE
            """,
            (self.company_id, budget_id),
        )
        if budget is None:
            raise NotFoundError(f"budget {budget_id!r} does not exist")
        self._assert_budget_usable(budget, timestamp)
        if expected_revision is not None and budget["revision"] != expected_revision:
            raise ConflictError(
                f"stale budget revision: expected {expected_revision}, current {budget['revision']}"
            )
        available = budget["limit"] - budget["spent"] - budget["reserved"]
        if amount_value > available:
            raise BudgetError(
                f"reservation exceeds available budget "
                f"({available} {ledger_label(budget['unit'], budget['currency'])})"
            )
        await self._execute(
            """
            UPDATE public.anc_budgets
            SET reserved_amount = reserved_amount + %s, revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND budget_id = %s
            """,
            (amount_value, timestamp, self.company_id, budget_id),
        )
        record = await self._fetchone(
            """
            INSERT INTO public.anc_budget_reservations
                (company_id, reservation_id, reservation_group_id, ledger_role,
                 budget_id, amount, actual_amount,
                 unit, currency, purpose, status, idempotency_key, metadata, revision,
                 expires_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, 'active', %s, %s, 0,
                    %s, %s, %s)
            RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                      budget_id, amount, actual_amount,
                      unit, currency, purpose, status, idempotency_key, metadata, revision,
                      expires_at, created_at, updated_at
            """,
            (
                self.company_id,
                reservation_id,
                reservation_id,
                "budget",
                budget_id,
                amount_value,
                budget["unit"],
                budget["currency"],
                purpose,
                idempotency_key,
                _jsonb(metadata_copy),
                expiry,
                timestamp,
                timestamp,
            ),
        )
        if record is None:
            raise RuntimeError("reservation insert returned no row")
        return record

    async def reserve_budget_group(
        self,
        *,
        reservation_group_id: str,
        budget_ids: RecordMapping,
        amount: Decimal | int | str,
        purpose: str,
        expected_revisions: Mapping[str, int] | None = None,
        expires_at: datetime | None = None,
        idempotency_key: str | None = None,
        metadata: RecordMapping | None = None,
        now: datetime | None = None,
    ) -> Record:
        reservation_group_id = _required(reservation_group_id, "reservation_group_id")
        if not isinstance(budget_ids, Mapping) or not budget_ids:
            raise ValidationError("budget_ids must be a non-empty mapping")
        normalized: dict[str, str] = {}
        for raw_role, raw_budget_id in budget_ids.items():
            role = _required(str(raw_role), "ledger_role").lower()
            if role in normalized:
                raise ValidationError(f"duplicate ledger role {role!r}")
            normalized[role] = _required(str(raw_budget_id), "budget_id")
        if len(set(normalized.values())) != len(normalized):
            raise ValidationError("each ledger role must identify a distinct budget")
        purpose = _required(purpose, "purpose")
        amount_value = _positive_money(amount)
        timestamp = _utc(now)
        expiry = _utc(expires_at) if expires_at is not None else None
        if expiry is not None and expiry <= timestamp:
            raise ValidationError("expires_at must be later than now")
        metadata_copy = _json_copy(metadata or {})
        revisions = dict(expected_revisions or {})
        unknown_revisions = set(revisions) - set(normalized.values())
        if unknown_revisions:
            raise ValidationError("expected_revisions contains an unknown budget")
        for value in revisions.values():
            _revision(value, required=True)

        await self._execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (json.dumps([self.company_id, "reservation-group", reservation_group_id]),),
        )
        existing = await self._get_reservation_group_members(reservation_group_id, for_update=True)
        if existing:
            expected_members = {
                role: (
                    budget_id,
                    _reservation_member_id(self.company_id, reservation_group_id, role),
                )
                for role, budget_id in normalized.items()
            }
            actual_members = {
                item["ledger_role"]: (item["budget_id"], item["reservation_id"])
                for item in existing
            }
            exact = (
                actual_members == expected_members
                and all(item["amount"] == amount_value for item in existing)
                and all(item["purpose"] == purpose for item in existing)
                and all(item["idempotency_key"] == idempotency_key for item in existing)
                and all(item["metadata"] == metadata_copy for item in existing)
                and all(item["expires_at"] == expiry for item in existing)
            )
            if exact:
                return _budget_group_record(existing)
            raise ConflictError(f"reservation_group_id {reservation_group_id!r} already exists")

        ordered_budget_ids = sorted(normalized.values())
        budgets = await self._fetchall(
            """
            SELECT company_id, budget_id, scope, parent_budget_id, stage,
                   limit_amount AS limit, spent_amount AS spent,
                   reserved_amount AS reserved, unit, currency, cell_id, agent_id,
                   experiment_id, status, revision, starts_at, ends_at, metadata,
                   created_at, updated_at
            FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = ANY(%s)
            ORDER BY budget_id
            FOR UPDATE
            """,
            (self.company_id, ordered_budget_ids),
        )
        by_id = {item["budget_id"]: item for item in budgets}
        missing = set(ordered_budget_ids) - set(by_id)
        if missing:
            raise NotFoundError(f"budget {sorted(missing)[0]!r} does not exist")
        by_role = {role: by_id[budget_id] for role, budget_id in normalized.items()}
        for role, budget in by_role.items():
            self._assert_budget_usable(budget, timestamp)
            expected = revisions.get(budget["budget_id"])
            if expected is not None and budget["revision"] != expected:
                raise ConflictError(
                    f"stale budget revision: expected {expected}, current {budget['revision']}"
                )
            available = budget["limit"] - budget["spent"] - budget["reserved"]
            if amount_value > available:
                raise BudgetError(
                    f"{role} reservation exceeds available budget "
                    f"({available} {ledger_label(budget['unit'], budget['currency'])})"
                )
        identities = {identity_of(budget) for budget in by_role.values()}
        if len(identities) != 1:
            raise BudgetError("all budgets in a reservation group must share a unit of account")

        reservations: list[Record] = []
        for role, budget_id in normalized.items():
            budget = by_role[role]
            await self._execute(
                """
                UPDATE public.anc_budgets
                SET reserved_amount = reserved_amount + %s,
                    revision = revision + 1, updated_at = %s
                WHERE company_id = %s AND budget_id = %s
                """,
                (amount_value, timestamp, self.company_id, budget_id),
            )
            reservation_id = _reservation_member_id(self.company_id, reservation_group_id, role)
            record = await self._fetchone(
                """
                INSERT INTO public.anc_budget_reservations
                    (company_id, reservation_id, reservation_group_id, ledger_role,
                     budget_id, amount, actual_amount, unit, currency, purpose, status,
                     idempotency_key, metadata, revision, expires_at, created_at,
                     updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, 'active',
                        %s, %s, 0, %s, %s, %s)
                RETURNING company_id, reservation_id, reservation_group_id,
                          ledger_role, budget_id, amount, actual_amount, unit, currency,
                          purpose, status, idempotency_key, metadata, revision,
                          expires_at, created_at, updated_at
                """,
                (
                    self.company_id,
                    reservation_id,
                    reservation_group_id,
                    role,
                    budget_id,
                    amount_value,
                    budget["unit"],
                    budget["currency"],
                    purpose,
                    idempotency_key,
                    _jsonb(metadata_copy),
                    expiry,
                    timestamp,
                    timestamp,
                ),
            )
            if record is None:
                raise RuntimeError("group reservation insert returned no row")
            reservations.append(record)
        return _budget_group_record(reservations)

    async def get_reservation(self, reservation_id: str) -> Record | None:
        return await self._get_reservation(_required(reservation_id, "reservation_id"))

    async def get_reservation_group(self, reservation_group_id: str) -> Record | None:
        reservation_group_id = _required(reservation_group_id, "reservation_group_id")
        reservations = await self._get_reservation_group_members(reservation_group_id)
        return _budget_group_record(reservations) if reservations else None

    async def _get_reservation_group_members(
        self,
        reservation_group_id: str,
        *,
        for_update: bool = False,
    ) -> list[Record]:
        suffix = " FOR UPDATE" if for_update else ""
        return await self._fetchall(
            """
            SELECT company_id, reservation_id, reservation_group_id, ledger_role,
                   budget_id, amount, actual_amount, unit, currency, purpose, status,
                   idempotency_key, metadata, revision, expires_at, created_at,
                   updated_at
            FROM public.anc_budget_reservations
            WHERE company_id = %s AND reservation_group_id = %s
            ORDER BY reservation_id
            """
            + suffix,
            (self.company_id, reservation_group_id),
        )

    async def _get_reservation(
        self,
        reservation_id: str,
        *,
        for_update: bool = False,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return await self._fetchone(
            """
            SELECT company_id, reservation_id, reservation_group_id, ledger_role,
                   budget_id, amount, actual_amount,
                   unit, currency, purpose, status, idempotency_key, metadata, revision,
                   expires_at, created_at, updated_at
            FROM public.anc_budget_reservations
            WHERE company_id = %s AND reservation_id = %s
            """
            + suffix,
            (self.company_id, reservation_id),
        )

    async def commit_reservation(
        self,
        *,
        reservation_id: str,
        actual_amount: Decimal | int | str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        reservation_id = _required(reservation_id, "reservation_id")
        reservation = await self._get_reservation(reservation_id, for_update=True)
        if reservation is None:
            raise NotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation["reservation_group_id"] != reservation_id:
            raise ConflictError("group reservation members must be committed through their group")
        actual = (
            _reservation_hold_amount(reservation)
            if actual_amount is None
            else _money(actual_amount, "actual_amount")
        )
        expected_revision = _revision(expected_revision)
        reserved_release, additional_spend = _reservation_commit_delta(reservation, actual)
        if reservation["status"] == "committed":
            if reservation["actual_amount"] == actual:
                return reservation
            raise ConflictError("reservation was committed with a different actual amount")
        if reservation["status"] != "active":
            raise ConflictError(f"cannot commit a {reservation['status']} reservation")
        if expected_revision is not None and reservation["revision"] != expected_revision:
            raise ConflictError(
                "stale reservation revision: "
                f"expected {expected_revision}, current {reservation['revision']}"
            )
        timestamp = _utc(now)
        await self._execute(
            """
            UPDATE public.anc_budgets
            SET reserved_amount = reserved_amount - %s,
                spent_amount = spent_amount + %s,
                revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND budget_id = %s
            """,
            (
                reserved_release,
                additional_spend,
                timestamp,
                self.company_id,
                reservation["budget_id"],
            ),
        )
        record = await self._fetchone(
            """
            UPDATE public.anc_budget_reservations
            SET status = 'committed', actual_amount = %s, revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND reservation_id = %s
            RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                      budget_id, amount, actual_amount,
                      unit, currency, purpose, status, idempotency_key, metadata, revision,
                      expires_at, created_at, updated_at
            """,
            (actual, timestamp, self.company_id, reservation_id),
        )
        if record is None:
            raise RuntimeError("reservation commit returned no row")
        return record

    async def commit_reservation_group(
        self,
        *,
        reservation_group_id: str,
        actual_amount: Decimal | int | str | None = None,
        expected_revisions: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> Record:
        reservation_group_id = _required(reservation_group_id, "reservation_group_id")
        members = await self._get_reservation_group_members(reservation_group_id, for_update=True)
        if not members:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
        group = _budget_group_record(members)
        actual = (
            _reservation_hold_amount(group)
            if actual_amount is None
            else _money(actual_amount, "actual_amount")
        )
        reserved_release, additional_spend = _reservation_commit_delta(group, actual)
        if group["status"] == "committed":
            if group["actual_amount"] == actual:
                return group
            raise ConflictError("reservation group was committed with a different actual amount")
        if group["status"] != "active":
            raise ConflictError(f"cannot commit a {group['status']} reservation group")
        revisions = dict(expected_revisions or {})
        member_ids = {item["reservation_id"] for item in members}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in members:
            expected = revisions.get(member["reservation_id"])
            if expected is not None:
                _revision(expected, required=True)
                if member["revision"] != expected:
                    raise ConflictError(
                        f"stale reservation revision: expected {expected}, "
                        f"current {member['revision']}"
                    )

        timestamp = _utc(now)
        budget_ids = sorted(item["budget_id"] for item in members)
        budgets = await self._fetchall(
            """
            SELECT budget_id FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = ANY(%s)
            ORDER BY budget_id FOR UPDATE
            """,
            (self.company_id, budget_ids),
        )
        if len(budgets) != len(budget_ids):
            raise ConflictError("reservation group references a missing budget")
        await self._execute(
            """
            UPDATE public.anc_budgets
            SET reserved_amount = reserved_amount - %s,
                spent_amount = spent_amount + %s,
                revision = revision + 1, updated_at = %s
            WHERE company_id = %s AND budget_id = ANY(%s)
            """,
            (reserved_release, additional_spend, timestamp, self.company_id, budget_ids),
        )
        updated = await self._fetchall(
            """
            UPDATE public.anc_budget_reservations
            SET status = 'committed', actual_amount = %s, revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND reservation_group_id = %s
            RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                      budget_id, amount, actual_amount, unit, currency, purpose, status,
                      idempotency_key, metadata, revision, expires_at, created_at,
                      updated_at
            """,
            (actual, timestamp, self.company_id, reservation_group_id),
        )
        return _budget_group_record(updated)

    async def apply_reservation_group_spend(
        self,
        *,
        reservation_group_id: str,
        amount: Decimal | int | str,
        expected_revisions: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> Record:
        reservation_group_id = _required(reservation_group_id, "reservation_group_id")
        members = await self._get_reservation_group_members(reservation_group_id, for_update=True)
        if not members:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
        group = _budget_group_record(members)
        increment = _money(amount, "amount")
        if increment <= 0:
            raise BudgetError("spend amount must be greater than zero")
        remaining = _reservation_remaining_hold(group)
        if increment > remaining:
            raise BudgetError("spend exceeds remaining reserved amount")
        if group["status"] != "active":
            raise ConflictError(f"cannot spend a {group['status']} reservation group")
        revisions = dict(expected_revisions or {})
        member_ids = {item["reservation_id"] for item in members}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in members:
            expected = revisions.get(member["reservation_id"])
            if expected is not None:
                _revision(expected, required=True)
                if member["revision"] != expected:
                    raise ConflictError(
                        f"stale reservation revision: expected {expected}, "
                        f"current {member['revision']}"
                    )

        timestamp = _utc(now)
        next_actual = _reservation_applied_amount(group) + increment
        budget_ids = sorted(item["budget_id"] for item in members)
        budgets = await self._fetchall(
            """
            SELECT budget_id FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = ANY(%s)
            ORDER BY budget_id FOR UPDATE
            """,
            (self.company_id, budget_ids),
        )
        if len(budgets) != len(budget_ids):
            raise ConflictError("reservation group references a missing budget")
        await self._execute(
            """
            UPDATE public.anc_budgets
            SET reserved_amount = reserved_amount - %s,
                spent_amount = spent_amount + %s,
                revision = revision + 1, updated_at = %s
            WHERE company_id = %s AND budget_id = ANY(%s)
            """,
            (increment, increment, timestamp, self.company_id, budget_ids),
        )
        try:
            updated = await self._fetchall(
                """
                UPDATE public.anc_budget_reservations
                SET actual_amount = %s, revision = revision + 1, updated_at = %s
                WHERE company_id = %s AND reservation_group_id = %s
                RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                          budget_id, amount, actual_amount, unit, currency, purpose, status,
                          idempotency_key, metadata, revision, expires_at, created_at,
                          updated_at
                """,
                (next_actual, timestamp, self.company_id, reservation_group_id),
            )
        except Exception as exc:
            if exc.__class__.__name__ == "CheckViolation":
                raise ConflictError("reservation spend rejected by ledger constraint") from exc
            raise
        return _budget_group_record(updated)

    async def adjust_reservation_group_hold(
        self,
        *,
        reservation_group_id: str,
        amount: Decimal | int | str,
        expected_revisions: Mapping[str, int] | None = None,
        metadata: RecordMapping | None = None,
        now: datetime | None = None,
    ) -> Record:
        reservation_group_id = _required(reservation_group_id, "reservation_group_id")
        members = await self._get_reservation_group_members(reservation_group_id, for_update=True)
        if not members:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
        group = _budget_group_record(members)
        if group["status"] != "active":
            raise ConflictError(f"cannot resize a {group['status']} reservation group")
        next_amount = _positive_money(amount)
        applied = _reservation_applied_amount(group)
        if next_amount < applied:
            raise BudgetError("reservation hold cannot drop below already applied spend")
        current = _reservation_hold_amount(group)
        revisions = dict(expected_revisions or {})
        member_ids = {item["reservation_id"] for item in members}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in members:
            expected = revisions.get(member["reservation_id"])
            if expected is not None:
                _revision(expected, required=True)
                if member["revision"] != expected:
                    raise ConflictError(
                        f"stale reservation revision: expected {expected}, "
                        f"current {member['revision']}"
                    )

        timestamp = _utc(now)
        delta = next_amount - current
        metadata_copy = None if metadata is None else _json_copy(metadata)
        if delta == 0 and metadata_copy is None:
            return group
        budget_ids = sorted(item["budget_id"] for item in members)
        locked = await self._fetchall(
            """
            SELECT budget_id, limit_amount AS limit,
                   spent_amount AS spent, reserved_amount AS reserved,
                   unit, currency, status, starts_at, ends_at
            FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = ANY(%s)
            ORDER BY budget_id FOR UPDATE
            """,
            (self.company_id, budget_ids),
        )
        if len(locked) != len(budget_ids):
            raise ConflictError("reservation group references a missing budget")
        by_id = {item["budget_id"]: item for item in locked}
        if delta > 0:
            for member in members:
                budget = by_id[member["budget_id"]]
                self._assert_budget_usable(budget, timestamp)
                available = budget["limit"] - budget["spent"] - budget["reserved"]
                if delta > available:
                    raise BudgetError(
                        f"{member['ledger_role']} reservation exceeds available budget "
                        f"({available} {ledger_label(budget['unit'], budget['currency'])})"
                    )
        if delta != 0:
            await self._execute(
                """
                UPDATE public.anc_budgets
                SET reserved_amount = reserved_amount + %s,
                    revision = revision + 1, updated_at = %s
                WHERE company_id = %s AND budget_id = ANY(%s)
                """,
                (delta, timestamp, self.company_id, budget_ids),
            )
        if metadata_copy is None:
            updated = await self._fetchall(
                """
                UPDATE public.anc_budget_reservations
                SET amount = %s, revision = revision + 1, updated_at = %s
                WHERE company_id = %s AND reservation_group_id = %s
                RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                          budget_id, amount, actual_amount, unit, currency, purpose, status,
                          idempotency_key, metadata, revision, expires_at, created_at,
                          updated_at
                """,
                (next_amount, timestamp, self.company_id, reservation_group_id),
            )
        else:
            updated = await self._fetchall(
                """
                UPDATE public.anc_budget_reservations
                SET amount = %s, metadata = %s, revision = revision + 1, updated_at = %s
                WHERE company_id = %s AND reservation_group_id = %s
                RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                          budget_id, amount, actual_amount, unit, currency, purpose, status,
                          idempotency_key, metadata, revision, expires_at, created_at,
                          updated_at
                """,
                (
                    next_amount,
                    _jsonb(metadata_copy),
                    timestamp,
                    self.company_id,
                    reservation_group_id,
                ),
            )
        return _budget_group_record(updated)

    async def release_reservation(
        self,
        *,
        reservation_id: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        reservation_id = _required(reservation_id, "reservation_id")
        reservation = await self._get_reservation(reservation_id, for_update=True)
        if reservation is None:
            raise NotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation["reservation_group_id"] != reservation_id:
            raise ConflictError("group reservation members must be released through their group")
        expected_revision = _revision(expected_revision)
        if reservation["status"] == "released":
            return reservation
        if reservation["status"] != "active":
            raise ConflictError(f"cannot release a {reservation['status']} reservation")
        if expected_revision is not None and reservation["revision"] != expected_revision:
            raise ConflictError(
                "stale reservation revision: "
                f"expected {expected_revision}, current {reservation['revision']}"
            )
        timestamp = _utc(now)
        await self._execute(
            """
            UPDATE public.anc_budgets
            SET reserved_amount = reserved_amount - %s, revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND budget_id = %s
            """,
            (
                _reservation_remaining_hold(reservation),
                timestamp,
                self.company_id,
                reservation["budget_id"],
            ),
        )
        record = await self._fetchone(
            """
            UPDATE public.anc_budget_reservations
            SET status = 'released', revision = revision + 1, updated_at = %s
            WHERE company_id = %s AND reservation_id = %s
            RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                      budget_id, amount, actual_amount,
                      unit, currency, purpose, status, idempotency_key, metadata, revision,
                      expires_at, created_at, updated_at
            """,
            (timestamp, self.company_id, reservation_id),
        )
        if record is None:
            raise RuntimeError("reservation release returned no row")
        return record

    async def release_reservation_group(
        self,
        *,
        reservation_group_id: str,
        expected_revisions: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> Record:
        reservation_group_id = _required(reservation_group_id, "reservation_group_id")
        members = await self._get_reservation_group_members(reservation_group_id, for_update=True)
        if not members:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
        group = _budget_group_record(members)
        if group["status"] == "released":
            return group
        if group["status"] != "active":
            raise ConflictError(f"cannot release a {group['status']} reservation group")
        revisions = dict(expected_revisions or {})
        member_ids = {item["reservation_id"] for item in members}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in members:
            expected = revisions.get(member["reservation_id"])
            if expected is not None:
                _revision(expected, required=True)
                if member["revision"] != expected:
                    raise ConflictError(
                        f"stale reservation revision: expected {expected}, "
                        f"current {member['revision']}"
                    )

        timestamp = _utc(now)
        budget_ids = sorted(item["budget_id"] for item in members)
        budgets = await self._fetchall(
            """
            SELECT budget_id FROM public.anc_budgets
            WHERE company_id = %s AND budget_id = ANY(%s)
            ORDER BY budget_id FOR UPDATE
            """,
            (self.company_id, budget_ids),
        )
        if len(budgets) != len(budget_ids):
            raise ConflictError("reservation group references a missing budget")
        await self._execute(
            """
            UPDATE public.anc_budgets
            SET reserved_amount = reserved_amount - %s,
                revision = revision + 1, updated_at = %s
            WHERE company_id = %s AND budget_id = ANY(%s)
            """,
            (_reservation_remaining_hold(group), timestamp, self.company_id, budget_ids),
        )
        updated = await self._fetchall(
            """
            UPDATE public.anc_budget_reservations
            SET status = 'released', revision = revision + 1, updated_at = %s
            WHERE company_id = %s AND reservation_group_id = %s
            RETURNING company_id, reservation_id, reservation_group_id, ledger_role,
                      budget_id, amount, actual_amount, unit, currency, purpose, status,
                      idempotency_key, metadata, revision, expires_at, created_at,
                      updated_at
            """,
            (timestamp, self.company_id, reservation_group_id),
        )
        return _budget_group_record(updated)

    @staticmethod
    def _assert_budget_usable(budget: Record, timestamp: datetime) -> None:
        if budget["status"] != "active":
            raise BudgetError(f"budget is {budget['status']}")
        if timestamp < budget["starts_at"]:
            raise BudgetError("budget has not started")
        if budget["ends_at"] is not None and timestamp >= budget["ends_at"]:
            raise BudgetError("budget has ended")

    async def acquire_lease(
        self,
        *,
        work_item_id: str,
        agent_id: str,
        agent_run_id: str | None = None,
        ttl: timedelta | int | float,
        lease_id: str | None = None,
        cell_id: str | None = None,
        token: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        work_item_id = _required(work_item_id, "work_item_id")
        agent_id = _required(agent_id, "agent_id")
        lease_id = _required(lease_id or str(uuid4()), "lease_id")
        token = _required(token or secrets.token_urlsafe(32), "token")
        timestamp = _utc(now)
        expires_at = timestamp + _ttl(ttl)
        expected_revision = _revision(expected_revision)
        # Serializes the absent-row case as well as replacements of expired leases.
        await self._execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (json.dumps([self.company_id, "work_lease", work_item_id]),),
        )
        existing = await self._get_lease(work_item_id, for_update=True)
        if (
            existing is not None
            and existing["status"] == "active"
            and existing["expires_at"] > timestamp
        ):
            if existing["lease_id"] == lease_id and existing["agent_id"] == agent_id:
                requested_run = str(agent_run_id) if agent_run_id is not None else None
                if existing["agent_run_id"] == requested_run:
                    return existing
                raise ConflictError("lease retry changed agent_run_id attribution")
            raise LeaseError(f"work item {work_item_id!r} already has an active lease")
        current_revision = existing["revision"] if existing is not None else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise ConflictError(
                f"stale lease revision: expected {expected_revision}, current {current_revision}"
            )
        if existing is None:
            record = await self._fetchone(
                """
                INSERT INTO public.anc_work_leases
                    (company_id, work_item_id, lease_id, agent_id, agent_run_id, cell_id,
                     token, status,
                     revision, acquired_at, expires_at, released_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', 0, %s, %s, NULL, %s)
                RETURNING company_id, work_item_id, lease_id, agent_id, agent_run_id,
                          cell_id, token, status,
                          revision, acquired_at, expires_at, released_at, updated_at
                """,
                (
                    self.company_id,
                    work_item_id,
                    lease_id,
                    agent_id,
                    str(agent_run_id) if agent_run_id is not None else None,
                    str(cell_id) if cell_id is not None else None,
                    token,
                    timestamp,
                    expires_at,
                    timestamp,
                ),
            )
        else:
            record = await self._fetchone(
                """
                UPDATE public.anc_work_leases
                SET lease_id = %s, agent_id = %s, agent_run_id = %s, cell_id = %s,
                    token = %s, status = 'active',
                    revision = revision + 1, acquired_at = %s, expires_at = %s,
                    released_at = NULL, updated_at = %s
                WHERE company_id = %s AND work_item_id = %s
                RETURNING company_id, work_item_id, lease_id, agent_id, agent_run_id,
                          cell_id, token, status,
                          revision, acquired_at, expires_at, released_at, updated_at
                """,
                (
                    lease_id,
                    agent_id,
                    str(agent_run_id) if agent_run_id is not None else None,
                    str(cell_id) if cell_id is not None else None,
                    token,
                    timestamp,
                    expires_at,
                    timestamp,
                    self.company_id,
                    work_item_id,
                ),
            )
        if record is None:
            raise RuntimeError("lease acquisition returned no row")
        return record

    async def get_lease(
        self,
        work_item_id: str,
        *,
        include_inactive: bool = False,
        now: datetime | None = None,
    ) -> Record | None:
        record = await self._get_lease(_required(work_item_id, "work_item_id"))
        if record is None:
            return None
        if not include_inactive:
            timestamp = _utc(now)
            if record["status"] != "active" or record["expires_at"] <= timestamp:
                return None
        return record

    async def _get_lease(self, work_item_id: str, *, for_update: bool = False) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return await self._fetchone(
            """
            SELECT company_id, work_item_id, lease_id, agent_id, agent_run_id, cell_id,
                   token, status,
                   revision, acquired_at, expires_at, released_at, updated_at
            FROM public.anc_work_leases
            WHERE company_id = %s AND work_item_id = %s
            """
            + suffix,
            (self.company_id, work_item_id),
        )

    async def renew_lease(
        self,
        *,
        work_item_id: str,
        token: str,
        ttl: timedelta | int | float,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        work_item_id = _required(work_item_id, "work_item_id")
        token = _required(token, "token")
        timestamp = _utc(now)
        expected_revision = _revision(expected_revision)
        record = await self._get_lease(work_item_id, for_update=True)
        if record is None:
            raise NotFoundError(f"lease for work item {work_item_id!r} does not exist")
        self._assert_active_lease(record, token, timestamp)
        if expected_revision is not None and record["revision"] != expected_revision:
            raise ConflictError(
                f"stale lease revision: expected {expected_revision}, current {record['revision']}"
            )
        expires_at = timestamp + _ttl(ttl)
        if expires_at <= record["expires_at"]:
            raise LeaseError("lease renewal must extend its current expiration")
        updated = await self._fetchone(
            """
            UPDATE public.anc_work_leases
            SET expires_at = %s, revision = revision + 1, updated_at = %s
            WHERE company_id = %s AND work_item_id = %s
            RETURNING company_id, work_item_id, lease_id, agent_id, agent_run_id,
                      cell_id, token, status,
                      revision, acquired_at, expires_at, released_at, updated_at
            """,
            (expires_at, timestamp, self.company_id, work_item_id),
        )
        if updated is None:
            raise RuntimeError("lease renewal returned no row")
        return updated

    async def release_lease(
        self,
        *,
        work_item_id: str,
        token: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        work_item_id = _required(work_item_id, "work_item_id")
        token = _required(token, "token")
        timestamp = _utc(now)
        expected_revision = _revision(expected_revision)
        record = await self._get_lease(work_item_id, for_update=True)
        if record is None:
            raise NotFoundError(f"lease for work item {work_item_id!r} does not exist")
        if record["status"] == "released" and secrets.compare_digest(record["token"], token):
            return record
        self._assert_active_lease(record, token, timestamp)
        if expected_revision is not None and record["revision"] != expected_revision:
            raise ConflictError(
                f"stale lease revision: expected {expected_revision}, current {record['revision']}"
            )
        updated = await self._fetchone(
            """
            UPDATE public.anc_work_leases
            SET status = 'released', released_at = %s, revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND work_item_id = %s
            RETURNING company_id, work_item_id, lease_id, agent_id, agent_run_id,
                      cell_id, token, status,
                      revision, acquired_at, expires_at, released_at, updated_at
            """,
            (timestamp, timestamp, self.company_id, work_item_id),
        )
        if updated is None:
            raise RuntimeError("lease release returned no row")
        return updated

    async def expire_lease(
        self,
        *,
        work_item_id: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        work_item_id = _required(work_item_id, "work_item_id")
        timestamp = _utc(now)
        expected_revision = _revision(expected_revision)
        record = await self._get_lease(work_item_id, for_update=True)
        if record is None:
            raise NotFoundError(f"lease for work item {work_item_id!r} does not exist")
        if record["status"] != "active":
            raise LeaseError(f"lease is {record['status']}")
        if expected_revision is not None and record["revision"] != expected_revision:
            raise ConflictError(
                f"stale lease revision: expected {expected_revision}, current {record['revision']}"
            )
        if record["expires_at"] > timestamp:
            raise LeaseError("lease has not expired")
        updated = await self._fetchone(
            """
            UPDATE public.anc_work_leases
            SET status = 'released', released_at = %s, revision = revision + 1,
                updated_at = %s
            WHERE company_id = %s AND work_item_id = %s
            RETURNING company_id, work_item_id, lease_id, agent_id, agent_run_id,
                      cell_id, token, status,
                      revision, acquired_at, expires_at, released_at, updated_at
            """,
            (timestamp, timestamp, self.company_id, work_item_id),
        )
        if updated is None:
            raise RuntimeError("lease expiry returned no row")
        return updated

    async def list_running_agent_runs(
        self,
        *,
        after_expires_at: datetime | None = None,
        after_agent_run_id: str | None = None,
        limit: int = 200,
    ) -> list[Record]:
        """Running runs for this company, keyset paged on (expires_at, agent_run_id)."""

        return await self._fetchall(
            """
            SELECT agent_run_id, agent_instance_id, role, cell_id, expires_at,
                   instance_status
            FROM public.anc_list_maintenance_agent_runs_page(%s, %s, %s, %s)
            """,
            (self.company_id, after_expires_at, after_agent_run_id, int(limit)),
        )

    async def end_cell_agent_runs(
        self,
        *,
        cell_id: str,
        termination_code: str,
        ended_at: datetime | None = None,
    ) -> list[str]:
        """End every running run of one cell inside this transaction."""

        cell_id = _required(cell_id, "cell_id")
        termination_code = _required(termination_code, "termination_code")
        rows = await self._fetchall(
            "SELECT agent_run_id FROM public.anc_end_cell_agent_runs(%s, %s, %s, %s)",
            (self.company_id, cell_id, termination_code, _utc(ended_at)),
        )
        return sorted(str(row["agent_run_id"]) for row in rows)

    @staticmethod
    def _assert_active_lease(record: Record, token: str, timestamp: datetime) -> None:
        if not secrets.compare_digest(record["token"], token):
            raise LeaseError("lease token does not match")
        if record["status"] != "active":
            raise LeaseError(f"lease is {record['status']}")
        if record["expires_at"] <= timestamp:
            raise LeaseError("lease has expired")

    async def list_events(
        self,
        *,
        after_position: int = 0,
        limit: int = 1_000,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> list[Record]:
        limit = _page(limit)
        if isinstance(after_position, bool) or not isinstance(after_position, int):
            raise ValidationError("after_position must be a non-negative integer")
        if after_position < 0:
            raise ValidationError("after_position must not be negative")
        return await self._fetchall(
            """
            SELECT event_id, company_id, aggregate_type, aggregate_id, event_type,
                   aggregate_revision, actor_id, cell_id, schema_version, occurred_at,
                   payload, metadata,
                   idempotency_key, correlation_id, causation_id, position
            FROM public.anc_domain_events
            WHERE company_id = %s AND position > %s
              AND (%s::text IS NULL OR aggregate_type = %s)
              AND (%s::text IS NULL OR aggregate_id = %s)
            ORDER BY position
            LIMIT %s
            """,
            (
                self.company_id,
                after_position,
                aggregate_type,
                aggregate_type,
                aggregate_id,
                aggregate_id,
                limit,
            ),
        )

    async def list_audit_events(
        self,
        *,
        after_position: int = 0,
        limit: int = 1_000,
    ) -> list[Record]:
        limit = _page(limit)
        if isinstance(after_position, bool) or not isinstance(after_position, int):
            raise ValidationError("after_position must be a non-negative integer")
        if after_position < 0:
            raise ValidationError("after_position must not be negative")
        return await self._fetchall(
            """
            SELECT audit_id, company_id, action, outcome, actor_id, resource_type,
                   resource_id, occurred_at, details, correlation_id, position
            FROM public.anc_audit_events
            WHERE company_id = %s AND position > %s
            ORDER BY position
            LIMIT %s
            """,
            (self.company_id, after_position, limit),
        )

    async def export(self) -> Record:
        domain_events = await self._fetchall(
            """
            SELECT event_id, company_id, aggregate_type, aggregate_id, event_type,
                   aggregate_revision, actor_id, cell_id, schema_version, occurred_at,
                   payload, metadata,
                   idempotency_key, correlation_id, causation_id, position
            FROM public.anc_domain_events
            WHERE company_id = %s ORDER BY position
            """,
            (self.company_id,),
        )
        audit_events = await self._fetchall(
            """
            SELECT audit_id, company_id, action, outcome, actor_id, resource_type,
                   resource_id, occurred_at, details, correlation_id, position
            FROM public.anc_audit_events
            WHERE company_id = %s ORDER BY position
            """,
            (self.company_id,),
        )
        projections = await self._fetchall(
            """
            SELECT company_id, projection_type, projection_id, revision, state,
                   created_at, updated_at
            FROM public.anc_projections
            WHERE company_id = %s ORDER BY projection_type, projection_id
            """,
            (self.company_id,),
        )
        idempotency = await self._fetchall(
            """
            SELECT company_id, namespace, idempotency_key AS key, request_hash,
                   status, response, error, revision, created_at, updated_at, expires_at
            FROM public.anc_idempotency_records
            WHERE company_id = %s ORDER BY namespace, idempotency_key
            """,
            (self.company_id,),
        )
        budgets = await self._fetchall(
            """
            SELECT company_id, budget_id, scope, parent_budget_id, stage,
                   limit_amount AS limit,
                   spent_amount AS spent, reserved_amount AS reserved, unit, currency,
                   cell_id, agent_id, experiment_id, status, revision, starts_at,
                   ends_at, metadata,
                   created_at, updated_at
            FROM public.anc_budgets
            WHERE company_id = %s ORDER BY budget_id
            """,
            (self.company_id,),
        )
        reservations = await self._fetchall(
            """
            SELECT company_id, reservation_id, reservation_group_id, ledger_role,
                   budget_id, amount, actual_amount,
                   unit, currency, purpose, status, idempotency_key, metadata, revision,
                   expires_at, created_at, updated_at
            FROM public.anc_budget_reservations
            WHERE company_id = %s ORDER BY reservation_id
            """,
            (self.company_id,),
        )
        leases = await self._fetchall(
            """
            SELECT company_id, work_item_id, lease_id, agent_id, agent_run_id, cell_id,
                   status,
                   revision, acquired_at, expires_at, released_at, updated_at
            FROM public.anc_work_leases
            WHERE company_id = %s ORDER BY work_item_id
            """,
            (self.company_id,),
        )
        return {
            "company_id": self.company_id,
            "exported_at": datetime.now(UTC),
            "domain_events": domain_events,
            "audit_events": audit_events,
            "projections": projections,
            "idempotency_records": _redact_export_secrets(idempotency),
            "budgets": budgets,
            "reservations": reservations,
            "work_leases": leases,
        }


__all__ = ["PostgresRepository"]

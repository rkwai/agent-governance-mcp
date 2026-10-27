"""Persistence contracts and a serializable in-memory implementation.

The persistence boundary intentionally deals in mappings instead of importing domain
models.  Domain services can therefore evolve their Pydantic models without coupling
the event store, projections, or PostgreSQL adapter to those modules.

Every transaction is scoped to exactly one company.  A caller cannot override the
scope on an individual operation.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar, runtime_checkable
from uuid import NAMESPACE_URL, uuid4, uuid5

from .errors import BudgetError, ConflictError, LeaseError, NotFoundError, ValidationError
from .ledger_units import (
    identity_of,
    ledger_label,
    normalize_ledger_identity,
)

type Record = dict[str, Any]
type RecordMapping = Mapping[str, Any]

T = TypeVar("T")
Reducer = Callable[[T, Record], T | Awaitable[T]]

MAX_PAGE_SIZE = 10_000


def utc_now() -> datetime:
    """Return an aware UTC timestamp.

    Kept as a function so deterministic tests and callers can supply explicit times
    without patching process-global clocks.
    """

    return datetime.now(UTC)


def _required(value: str, field_name: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValidationError(f"{field_name} must not be empty")
    return value


def _utc(value: datetime | None, *, default: datetime | None = None) -> datetime:
    resolved = value if value is not None else default
    if resolved is None:
        resolved = utc_now()
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValidationError("timestamps must include a UTC offset")
    return resolved.astimezone(UTC)


def _money(value: Decimal | int | str, field_name: str = "amount") -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValidationError(f"{field_name} must be a Decimal, integer, or decimal string")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} is not a valid decimal amount") from exc
    if not amount.is_finite():
        raise ValidationError(f"{field_name} must be finite")
    return amount


def _positive_money(value: Decimal | int | str, field_name: str = "amount") -> Decimal:
    amount = _money(value, field_name)
    if amount <= 0:
        raise ValidationError(f"{field_name} must be greater than zero")
    return amount


def _copy_record(record: RecordMapping | None) -> Record | None:
    return copy.deepcopy(dict(record)) if record is not None else None


def _copy_records(records: list[Record]) -> list[Record]:
    return copy.deepcopy(records)


_EXPORT_SECRET_KEYS = frozenset(
    {"authorization", "credentials", "lease_token", "password", "secret", "token"}
)


def _redact_export_secrets(value: Any) -> Any:
    """Remove bearer capabilities from diagnostic/export copies recursively."""

    if isinstance(value, Mapping):
        return {
            str(key): _redact_export_secrets(child)
            for key, child in value.items()
            if str(key).casefold().replace("-", "_") not in _EXPORT_SECRET_KEYS
        }
    if isinstance(value, list):
        return [_redact_export_secrets(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact_export_secrets(child) for child in value)
    return copy.deepcopy(value)


def _ttl(value: timedelta | int | float) -> timedelta:
    if isinstance(value, bool):
        raise ValidationError("lease ttl must be a duration or number of seconds")
    result = value if isinstance(value, timedelta) else timedelta(seconds=value)
    if result <= timedelta(0):
        raise ValidationError("lease ttl must be greater than zero")
    return result


def _page(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValidationError("limit must be an integer")
    if limit <= 0 or limit > MAX_PAGE_SIZE:
        raise ValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return limit


def _revision(value: int | None, *, required: bool = False) -> int | None:
    if value is None:
        if required:
            raise ValidationError("expected_revision is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("expected_revision must be a non-negative integer")
    return value


def _reservation_member_id(company_id: str, reservation_group_id: str, ledger_role: str) -> str:
    """Return a stable opaque ID for one member of a logical budget hold."""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"ai-nature-company:{company_id}:budget-reservation:{reservation_group_id}:{ledger_role}",
        )
    )


def _reservation_applied_amount(record: RecordMapping) -> Decimal:
    """Return spend already applied against an active or settled reservation."""

    applied = record.get("actual_amount")
    if applied is None:
        return Decimal(0)
    return _money(applied, "actual_amount")


def _reservation_hold_amount(record: RecordMapping) -> Decimal:
    return _money(record["amount"], "amount")


def _reservation_remaining_hold(record: RecordMapping) -> Decimal:
    remaining = _reservation_hold_amount(record) - _reservation_applied_amount(record)
    if remaining < 0:
        raise ConflictError("reservation applied spend exceeds the reserved hold")
    return remaining


def _reservation_commit_delta(record: RecordMapping, actual: Decimal) -> tuple[Decimal, Decimal]:
    """Return ``(reserved_release, additional_spend)`` to settle to ``actual``."""

    already = _reservation_applied_amount(record)
    hold = _reservation_hold_amount(record)
    if actual < 0 or actual > hold:
        raise BudgetError("actual_amount must be between zero and the reserved amount")
    if actual < already:
        raise BudgetError("actual_amount cannot be below already applied spend")
    return hold - already, actual - already


def _budget_group_record(reservations: list[Record]) -> Record:
    """Build the public logical-hold view from its per-ledger reservations."""

    if not reservations:
        raise ValueError("a budget reservation group must have at least one member")
    ordered = sorted(reservations, key=lambda item: item["ledger_role"])
    first = ordered[0]
    statuses = {item["status"] for item in ordered}
    identities = {identity_of(item) for item in ordered}
    amounts = {item["amount"] for item in ordered}
    actual_amounts = {item["actual_amount"] for item in ordered}
    group_ids = {item["reservation_group_id"] for item in ordered}
    company_ids = {item["company_id"] for item in ordered}
    ledger_roles = [item["ledger_role"] for item in ordered]
    group_fields_match = all(
        item["purpose"] == first["purpose"]
        and item["idempotency_key"] == first["idempotency_key"]
        and item["metadata"] == first["metadata"]
        and item["expires_at"] == first["expires_at"]
        for item in ordered
    )
    if (
        len(statuses) != 1
        or len(identities) != 1
        or len(amounts) != 1
        or len(actual_amounts) != 1
        or len(group_ids) != 1
        or len(company_ids) != 1
        or len(set(ledger_roles)) != len(ledger_roles)
        or not group_fields_match
    ):
        raise ConflictError("budget reservation group members are inconsistent")
    return {
        "reservation_group_id": first["reservation_group_id"],
        "company_id": first["company_id"],
        "amount": first["amount"],
        "actual_amount": first["actual_amount"],
        "unit": first["unit"],
        "currency": first["currency"],
        "purpose": first["purpose"],
        "status": first["status"],
        "idempotency_key": first["idempotency_key"],
        "metadata": copy.deepcopy(first["metadata"]),
        "expires_at": first["expires_at"],
        "created_at": min(item["created_at"] for item in ordered),
        "updated_at": max(item["updated_at"] for item in ordered),
        "reservation_ids": {item["ledger_role"]: item["reservation_id"] for item in ordered},
        "budget_ids": {item["ledger_role"]: item["budget_id"] for item in ordered},
        "members": copy.deepcopy(ordered),
    }


@runtime_checkable
class StoreTransaction(Protocol):
    """Company-scoped operations required from a persistence transaction."""

    @property
    def company_id(self) -> str: ...

    async def append_event(self, **fields: Any) -> Record: ...

    async def append_audit_event(self, **fields: Any) -> Record: ...

    async def get_projection(self, projection_type: str, projection_id: str) -> Record | None: ...

    async def list_projections(self, **filters: Any) -> list[Record]: ...

    async def put_projection(self, **fields: Any) -> Record: ...

    async def claim_idempotency(self, **fields: Any) -> Record: ...

    async def get_idempotency(self, namespace: str, key: str) -> Record | None: ...

    async def complete_idempotency(self, **fields: Any) -> Record: ...

    async def fail_idempotency(self, **fields: Any) -> Record: ...

    async def create_budget(self, **fields: Any) -> Record: ...

    async def get_budget(self, budget_id: str) -> Record | None: ...

    async def list_budgets(self, **filters: Any) -> list[Record]: ...

    async def update_budget(self, **fields: Any) -> Record: ...

    async def reserve_budget(self, **fields: Any) -> Record: ...

    async def reserve_budget_group(self, **fields: Any) -> Record: ...

    async def get_reservation(self, reservation_id: str) -> Record | None: ...

    async def get_reservation_group(self, reservation_group_id: str) -> Record | None: ...

    async def commit_reservation(self, **fields: Any) -> Record: ...

    async def commit_reservation_group(self, **fields: Any) -> Record: ...

    async def apply_reservation_group_spend(self, **fields: Any) -> Record: ...

    async def adjust_reservation_group_hold(self, **fields: Any) -> Record: ...

    async def release_reservation(self, **fields: Any) -> Record: ...

    async def release_reservation_group(self, **fields: Any) -> Record: ...

    async def acquire_lease(self, **fields: Any) -> Record: ...

    async def get_lease(
        self, work_item_id: str, *, include_inactive: bool = False
    ) -> Record | None: ...

    async def renew_lease(self, **fields: Any) -> Record: ...

    async def expire_lease(self, **fields: Any) -> Record: ...

    async def release_lease(self, **fields: Any) -> Record: ...

    async def list_running_agent_runs(self, **filters: Any) -> list[Record]: ...

    async def end_cell_agent_runs(self, **fields: Any) -> list[str]: ...

    async def list_events(self, **filters: Any) -> list[Record]: ...

    async def list_audit_events(self, **filters: Any) -> list[Record]: ...

    async def export(self) -> Record: ...


class TransactionContext(Protocol):
    async def __aenter__(self) -> StoreTransaction: ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool | None: ...


@runtime_checkable
class Repository(Protocol):
    """Minimal repository interface consumed by deterministic domain services."""

    def transaction(self, company_id: str) -> TransactionContext: ...

    async def export_company(self, company_id: str) -> Record: ...

    async def replay(
        self,
        company_id: str,
        reducer: Reducer[T],
        initial: T,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> T: ...


class TransactionalRepository:
    """Convenience operations shared by memory and PostgreSQL repositories."""

    def transaction(self, company_id: str) -> TransactionContext:
        raise NotImplementedError

    async def execute(
        self,
        company_id: str,
        operation: Callable[[StoreTransaction], Awaitable[T]],
    ) -> T:
        """Run an arbitrary operation atomically in one company scope."""

        async with self.transaction(company_id) as transaction:
            return await operation(transaction)

    async def export_company(self, company_id: str) -> Record:
        async with self.transaction(company_id) as transaction:
            return await transaction.export()

    async def replay(
        self,
        company_id: str,
        reducer: Reducer[T],
        initial: T,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> T:
        """Fold ordered domain events into a caller-owned state object."""

        state = initial
        position = 0
        while True:
            async with self.transaction(company_id) as transaction:
                page = await transaction.list_events(
                    after_position=position,
                    limit=MAX_PAGE_SIZE,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                )
            if not page:
                return state
            for event in page:
                result = reducer(state, event)
                state = await result if inspect.isawaitable(result) else result
            position = page[-1]["position"]
            if len(page) < MAX_PAGE_SIZE:
                return state


@dataclass
class _MemoryState:
    event_position: int = 0
    audit_position: int = 0
    events: list[Record] = field(default_factory=list)
    event_ids: dict[tuple[str, str], Record] = field(default_factory=dict)
    stream_revisions: dict[tuple[str, str, str], int] = field(default_factory=dict)
    audit_events: list[Record] = field(default_factory=list)
    audit_ids: dict[tuple[str, str], Record] = field(default_factory=dict)
    projections: dict[tuple[str, str, str], Record] = field(default_factory=dict)
    idempotency: dict[tuple[str, str, str], Record] = field(default_factory=dict)
    budgets: dict[tuple[str, str], Record] = field(default_factory=dict)
    reservations: dict[tuple[str, str], Record] = field(default_factory=dict)
    leases: dict[tuple[str, str], Record] = field(default_factory=dict)
    agent_runs: dict[tuple[str, str], Record] = field(default_factory=dict)


class InMemoryStore(TransactionalRepository):
    """Serializable, rollback-capable store intended for tests and local demos.

    The lock is deliberately held for the full transaction.  This provides behavior
    equivalent to a serializable single-database transaction, including atomic budget
    and lease checks, rather than the weaker behavior of independently locked methods.
    """

    def __init__(self) -> None:
        self._state = _MemoryState()
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None

    def seed_agent_run(
        self,
        company_id: str,
        *,
        agent_run_id: str,
        agent_instance_id: str,
        role: str,
        cell_id: str | None,
        started_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Test/demo hook: issue a running agent run (Postgres issues these at auth)."""

        self._state.agent_runs[(company_id, agent_run_id)] = {
            "company_id": company_id,
            "agent_run_id": agent_run_id,
            "agent_instance_id": agent_instance_id,
            "role": role,
            "cell_id": cell_id,
            "status": "running",
            "termination_code": None,
            "revision": 0,
            "started_at": _utc(started_at),
            "expires_at": _utc(expires_at),
            "ended_at": None,
            "updated_at": _utc(started_at),
        }

    def agent_run(self, company_id: str, agent_run_id: str) -> Record | None:
        record = self._state.agent_runs.get((company_id, agent_run_id))
        return copy.deepcopy(record) if record is not None else None

    @asynccontextmanager
    async def transaction(self, company_id: str) -> AsyncIterator[StoreTransaction]:
        company_id = _required(company_id, "company_id")
        task = asyncio.current_task()
        if task is not None and task is self._owner:
            raise RuntimeError("nested in-memory transactions are not supported")
        await self._lock.acquire()
        self._owner = task
        working = copy.deepcopy(self._state)
        transaction = _MemoryTransaction(working, company_id)
        try:
            yield transaction
        except BaseException:
            transaction._close()
            raise
        else:
            transaction._close()
            self._state = working
        finally:
            self._owner = None
            self._lock.release()


class _MemoryTransaction:
    def __init__(self, state: _MemoryState, company_id: str) -> None:
        self._state = state
        self._company_id = company_id
        self._open = True

    @property
    def company_id(self) -> str:
        return self._company_id

    def _close(self) -> None:
        self._open = False

    def _check_open(self) -> None:
        if not self._open:
            raise RuntimeError("transaction is closed")

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
        self._check_open()
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
        key = (self.company_id, event_id)
        existing = self._state.event_ids.get(key)
        immutable = {
            "company_id": self.company_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "cell_id": str(cell_id) if cell_id is not None else None,
            "schema_version": schema_version,
            "payload": copy.deepcopy(dict(payload)),
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "metadata": copy.deepcopy(dict(metadata or {})),
        }
        if existing is not None:
            if all(existing[field] == value for field, value in immutable.items()):
                return copy.deepcopy(existing)
            raise ConflictError(f"event_id {event_id!r} already identifies another event")

        stream_key = (self.company_id, aggregate_type, aggregate_id)
        current_revision = self._state.stream_revisions.get(stream_key, 0)
        if expected_revision is not None and expected_revision != current_revision:
            raise ConflictError(
                "stale aggregate revision: "
                f"expected {expected_revision}, current {current_revision}"
            )

        revision = current_revision + 1
        self._state.event_position += 1
        record: Record = {
            "event_id": event_id,
            **immutable,
            "aggregate_revision": revision,
            "position": self._state.event_position,
            "occurred_at": _utc(occurred_at),
        }
        self._state.events.append(record)
        self._state.event_ids[key] = record
        self._state.stream_revisions[stream_key] = revision
        return copy.deepcopy(record)

    async def append_domain_event(self, **fields: Any) -> Record:
        """Explicit alias useful at call sites that also append audit events."""

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
        self._check_open()
        audit_id = _required(audit_id or str(uuid4()), "audit_id")
        correlation_id = str(correlation_id) if correlation_id is not None else None
        immutable: Record = {
            "company_id": self.company_id,
            "action": _required(action, "action"),
            "outcome": _required(outcome, "outcome"),
            "actor_id": _required(actor_id, "actor_id"),
            "resource_type": _required(resource_type, "resource_type"),
            "resource_id": _required(resource_id, "resource_id"),
            "details": copy.deepcopy(dict(details or {})),
            "correlation_id": correlation_id,
        }
        key = (self.company_id, audit_id)
        existing = self._state.audit_ids.get(key)
        if existing is not None:
            if all(existing[field] == value for field, value in immutable.items()):
                return copy.deepcopy(existing)
            raise ConflictError(f"audit_id {audit_id!r} already identifies another event")

        self._state.audit_position += 1
        record = {
            "audit_id": audit_id,
            **immutable,
            "position": self._state.audit_position,
            "occurred_at": _utc(occurred_at),
        }
        self._state.audit_events.append(record)
        self._state.audit_ids[key] = record
        return copy.deepcopy(record)

    async def get_projection(self, projection_type: str, projection_id: str) -> Record | None:
        self._check_open()
        key = (
            self.company_id,
            _required(projection_type, "projection_type"),
            _required(projection_id, "projection_id"),
        )
        return _copy_record(self._state.projections.get(key))

    async def put_projection(
        self,
        *,
        projection_type: str,
        projection_id: str,
        state: RecordMapping,
        expected_revision: int,
        updated_at: datetime | None = None,
    ) -> Record:
        self._check_open()
        expected_revision = _revision(expected_revision, required=True)
        assert expected_revision is not None
        projection_type = _required(projection_type, "projection_type")
        projection_id = _required(projection_id, "projection_id")
        key = (self.company_id, projection_type, projection_id)
        existing = self._state.projections.get(key)
        current_revision = existing["revision"] if existing is not None else 0
        if expected_revision != current_revision:
            raise ConflictError(
                "stale projection revision: "
                f"expected {expected_revision}, current {current_revision}"
            )
        timestamp = _utc(updated_at)
        record: Record = {
            "company_id": self.company_id,
            "projection_type": projection_type,
            "projection_id": projection_id,
            "revision": current_revision + 1,
            "state": copy.deepcopy(dict(state)),
            "created_at": existing["created_at"] if existing is not None else timestamp,
            "updated_at": timestamp,
        }
        self._state.projections[key] = record
        return copy.deepcopy(record)

    async def list_projections(
        self,
        *,
        projection_type: str | None = None,
        after_projection_id: str | None = None,
        cell_id: str | None = None,
        limit: int = 1_000,
    ) -> list[Record]:
        """List projections in a stable order without escaping company scope."""

        self._check_open()
        limit = _page(limit)
        if projection_type is not None:
            projection_type = _required(projection_type, "projection_type")
        records = sorted(
            (
                record
                for (company, kind, _), record in self._state.projections.items()
                if company == self.company_id
                and (projection_type is None or kind == projection_type)
                and (after_projection_id is None or record["projection_id"] > after_projection_id)
                and (cell_id is None or record["state"].get("cell_id") == cell_id)
            ),
            key=lambda record: (record["projection_type"], record["projection_id"]),
        )
        return _copy_records(records[:limit])

    async def claim_idempotency(
        self,
        *,
        namespace: str,
        key: str,
        request_hash: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        namespace = _required(namespace, "namespace")
        key = _required(key, "key")
        request_hash = _required(request_hash, "request_hash")
        timestamp = _utc(now)
        expiry = _utc(expires_at) if expires_at is not None else None
        if expiry is not None and expiry <= timestamp:
            raise ValidationError("expires_at must be later than now")
        record_key = (self.company_id, namespace, key)
        existing = self._state.idempotency.get(record_key)
        if existing is not None and (
            existing["expires_at"] is None or existing["expires_at"] > timestamp
        ):
            if existing["request_hash"] != request_hash:
                raise ConflictError("idempotency key was already used for a different request")
            return copy.deepcopy(existing)

        revision = existing["revision"] + 1 if existing is not None else 1
        record: Record = {
            "company_id": self.company_id,
            "namespace": namespace,
            "key": key,
            "request_hash": request_hash,
            "status": "in_progress",
            "response": None,
            "error": None,
            "revision": revision,
            "created_at": timestamp,
            "updated_at": timestamp,
            "expires_at": expiry,
        }
        self._state.idempotency[record_key] = record
        return copy.deepcopy(record)

    async def begin_idempotency(self, **fields: Any) -> Record:
        return await self.claim_idempotency(**fields)

    async def get_idempotency(self, namespace: str, key: str) -> Record | None:
        self._check_open()
        record_key = (
            self.company_id,
            _required(namespace, "namespace"),
            _required(key, "key"),
        )
        return _copy_record(self._state.idempotency.get(record_key))

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
        self._check_open()
        namespace = _required(namespace, "namespace")
        key = _required(key, "key")
        request_hash = _required(request_hash, "request_hash")
        record_key = (self.company_id, namespace, key)
        existing = self._state.idempotency.get(record_key)
        if existing is None:
            raise NotFoundError("idempotency claim does not exist")
        if existing["request_hash"] != request_hash:
            raise ConflictError("idempotency key belongs to a different request")
        target_field = "response" if status == "completed" else "error"
        copied_value = copy.deepcopy(dict(value))
        if existing["status"] == status:
            if existing[target_field] == copied_value:
                return copy.deepcopy(existing)
            raise ConflictError("idempotency outcome is already immutable")
        if existing["status"] != "in_progress":
            raise ConflictError(f"idempotency claim is already {existing['status']}")

        record = copy.deepcopy(existing)
        record["status"] = status
        record[target_field] = copied_value
        record["updated_at"] = _utc(now)
        record["revision"] += 1
        self._state.idempotency[record_key] = record
        return copy.deepcopy(record)

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
        self._check_open()
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
        parent = None
        if parent_budget_id is not None:
            parent = self._state.budgets.get((self.company_id, parent_budget_id))
            if parent is None:
                raise NotFoundError(f"parent budget {parent_budget_id!r} does not exist")
            if identity_of(parent) != (unit, currency):
                raise ValidationError("child budget unit of account must match its parent")
            if start < parent["starts_at"] or (
                parent["ends_at"] is not None and (end is None or end > parent["ends_at"])
            ):
                raise ValidationError("child budget window must fit inside its parent")
        record: Record = {
            "budget_id": budget_id,
            "company_id": self.company_id,
            "scope": scope,
            "parent_budget_id": parent_budget_id,
            "stage": stage,
            "limit": limit_amount,
            "spent": Decimal(0),
            "reserved": Decimal(0),
            "unit": unit,
            "currency": currency,
            "cell_id": str(cell_id) if cell_id is not None else None,
            "agent_id": str(agent_id) if agent_id is not None else None,
            "experiment_id": str(experiment_id) if experiment_id is not None else None,
            "status": _required(status, "status"),
            "revision": 0,
            "starts_at": start,
            "ends_at": end,
            "metadata": copy.deepcopy(dict(metadata or {})),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        key = (self.company_id, budget_id)
        existing = self._state.budgets.get(key)
        if existing is not None:
            comparable = {
                name: record[name] for name in record if name not in {"created_at", "updated_at"}
            }
            old = {name: existing[name] for name in comparable}
            if comparable == old:
                return copy.deepcopy(existing)
            raise ConflictError(f"budget_id {budget_id!r} already exists")
        self._state.budgets[key] = record
        return copy.deepcopy(record)

    async def get_budget(self, budget_id: str) -> Record | None:
        self._check_open()
        key = (self.company_id, _required(budget_id, "budget_id"))
        return _copy_record(self._state.budgets.get(key))

    async def list_budgets(
        self,
        *,
        scope: str | None = None,
        cell_id: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        limit: int = 1_000,
    ) -> list[Record]:
        self._check_open()
        limit = _page(limit)
        scope = _required(scope, "scope") if scope is not None else None
        cell_id = _required(cell_id, "cell_id") if cell_id is not None else None
        stage = _required(stage, "stage").lower() if stage is not None else None
        status = _required(status, "status") if status is not None else None
        records = [
            budget
            for budget in self._state.budgets.values()
            if budget["company_id"] == self.company_id
            and (scope is None or budget["scope"] == scope)
            and (cell_id is None or budget["cell_id"] == cell_id)
            and (stage is None or budget["stage"] == stage)
            and (status is None or budget["status"] == status)
        ]
        records.sort(key=lambda item: item["budget_id"])
        return _copy_records(records[:limit])

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

        self._check_open()
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
        key = (self.company_id, budget_id)
        budget = self._state.budgets.get(key)
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
        budget["limit"] = next_limit
        if status is not None:
            budget["status"] = status
        if metadata is not None:
            budget["metadata"] = copy.deepcopy(dict(metadata))
        budget["revision"] += 1
        budget["updated_at"] = timestamp
        return copy.deepcopy(budget)

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
        self._check_open()
        budget_id = _required(budget_id, "budget_id")
        reservation_id = _required(reservation_id, "reservation_id")
        purpose = _required(purpose, "purpose")
        amount_value = _positive_money(amount)
        expected_revision = _revision(expected_revision)
        timestamp = _utc(now)
        expiry = _utc(expires_at) if expires_at is not None else None
        if expiry is not None and expiry <= timestamp:
            raise ValidationError("expires_at must be later than now")
        reservation_key = (self.company_id, reservation_id)
        existing = self._state.reservations.get(reservation_key)
        immutable = {
            "budget_id": budget_id,
            "reservation_group_id": reservation_id,
            "ledger_role": "budget",
            "amount": amount_value,
            "purpose": purpose,
            "idempotency_key": idempotency_key,
            "metadata": copy.deepcopy(dict(metadata or {})),
        }
        if existing is not None:
            if all(existing[field] == value for field, value in immutable.items()):
                return copy.deepcopy(existing)
            raise ConflictError(f"reservation_id {reservation_id!r} already exists")

        budget_key = (self.company_id, budget_id)
        budget = self._state.budgets.get(budget_key)
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

        budget["reserved"] += amount_value
        budget["revision"] += 1
        budget["updated_at"] = timestamp
        record: Record = {
            "reservation_id": reservation_id,
            "company_id": self.company_id,
            **immutable,
            "unit": budget["unit"],
            "currency": budget["currency"],
            "status": "active",
            "actual_amount": None,
            "revision": 0,
            "expires_at": expiry,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._state.reservations[reservation_key] = record
        return copy.deepcopy(record)

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
        """Atomically place one economic hold on every constraint ledger.

        The same amount is mirrored on each ledger.  It is one logical cost, not
        an amount to sum across the returned reservation members.
        """

        self._check_open()
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
        metadata_copy = copy.deepcopy(dict(metadata or {}))
        revisions = dict(expected_revisions or {})
        unknown_revisions = set(revisions) - set(normalized.values())
        if unknown_revisions:
            raise ValidationError("expected_revisions contains an unknown budget")
        for value in revisions.values():
            _revision(value, required=True)

        existing = [
            item
            for item in self._state.reservations.values()
            if item["company_id"] == self.company_id
            and item["reservation_group_id"] == reservation_group_id
        ]
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

        budgets: dict[str, Record] = {}
        for role, budget_id in sorted(normalized.items(), key=lambda item: item[1]):
            budget = self._state.budgets.get((self.company_id, budget_id))
            if budget is None:
                raise NotFoundError(f"budget {budget_id!r} does not exist")
            self._assert_budget_usable(budget, timestamp)
            expected = revisions.get(budget_id)
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
            budgets[role] = budget
        identities = {identity_of(budget) for budget in budgets.values()}
        if len(identities) != 1:
            raise BudgetError("all budgets in a reservation group must share a unit of account")

        reservations: list[Record] = []
        for role, budget_id in normalized.items():
            budget = budgets[role]
            budget["reserved"] += amount_value
            budget["revision"] += 1
            budget["updated_at"] = timestamp
            reservation_id = _reservation_member_id(self.company_id, reservation_group_id, role)
            record: Record = {
                "reservation_id": reservation_id,
                "reservation_group_id": reservation_group_id,
                "ledger_role": role,
                "company_id": self.company_id,
                "budget_id": budget_id,
                "amount": amount_value,
                "purpose": purpose,
                "idempotency_key": idempotency_key,
                "metadata": copy.deepcopy(metadata_copy),
                "unit": budget["unit"],
                "currency": budget["currency"],
                "status": "active",
                "actual_amount": None,
                "revision": 0,
                "expires_at": expiry,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            self._state.reservations[(self.company_id, reservation_id)] = record
            reservations.append(record)
        return _budget_group_record(reservations)

    async def get_reservation(self, reservation_id: str) -> Record | None:
        self._check_open()
        key = (self.company_id, _required(reservation_id, "reservation_id"))
        return _copy_record(self._state.reservations.get(key))

    async def get_reservation_group(self, reservation_group_id: str) -> Record | None:
        self._check_open()
        reservation_group_id = _required(reservation_group_id, "reservation_group_id")
        reservations = [
            item
            for item in self._state.reservations.values()
            if item["company_id"] == self.company_id
            and item["reservation_group_id"] == reservation_group_id
        ]
        return _budget_group_record(reservations) if reservations else None

    async def commit_reservation(
        self,
        *,
        reservation_id: str,
        actual_amount: Decimal | int | str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        reservation_id = _required(reservation_id, "reservation_id")
        reservation_key = (self.company_id, reservation_id)
        reservation = self._state.reservations.get(reservation_key)
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
                return copy.deepcopy(reservation)
            raise ConflictError("reservation was committed with a different actual amount")
        if reservation["status"] != "active":
            raise ConflictError(f"cannot commit a {reservation['status']} reservation")
        if expected_revision is not None and reservation["revision"] != expected_revision:
            raise ConflictError(
                "stale reservation revision: "
                f"expected {expected_revision}, current {reservation['revision']}"
            )

        timestamp = _utc(now)
        budget = self._state.budgets[(self.company_id, reservation["budget_id"])]
        budget["reserved"] -= reserved_release
        budget["spent"] += additional_spend
        budget["revision"] += 1
        budget["updated_at"] = timestamp
        reservation["status"] = "committed"
        reservation["actual_amount"] = actual
        reservation["revision"] += 1
        reservation["updated_at"] = timestamp
        return copy.deepcopy(reservation)

    async def commit_reservation_group(
        self,
        *,
        reservation_group_id: str,
        actual_amount: Decimal | int | str | None = None,
        expected_revisions: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        group = await self.get_reservation_group(reservation_group_id)
        if group is None:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
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
        member_ids = {item["reservation_id"] for item in group["members"]}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in group["members"]:
            expected = revisions.get(member["reservation_id"])
            if expected is not None:
                _revision(expected, required=True)
                if member["revision"] != expected:
                    raise ConflictError(
                        f"stale reservation revision: expected {expected}, "
                        f"current {member['revision']}"
                    )

        timestamp = _utc(now)
        updated: list[Record] = []
        for member in group["members"]:
            reservation = self._state.reservations[(self.company_id, member["reservation_id"])]
            budget = self._state.budgets[(self.company_id, reservation["budget_id"])]
            budget["reserved"] -= reserved_release
            budget["spent"] += additional_spend
            budget["revision"] += 1
            budget["updated_at"] = timestamp
            reservation["status"] = "committed"
            reservation["actual_amount"] = actual
            reservation["revision"] += 1
            reservation["updated_at"] = timestamp
            updated.append(reservation)
        return _budget_group_record(updated)

    async def apply_reservation_group_spend(
        self,
        *,
        reservation_group_id: str,
        amount: Decimal | int | str,
        expected_revisions: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> Record:
        """Move ``amount`` from reserved to spent on an active group without closing it."""

        self._check_open()
        group = await self.get_reservation_group(reservation_group_id)
        if group is None:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
        increment = _money(amount, "amount")
        if increment <= 0:
            raise BudgetError("spend amount must be greater than zero")
        remaining = _reservation_remaining_hold(group)
        if increment > remaining:
            raise BudgetError("spend exceeds remaining reserved amount")
        if group["status"] != "active":
            raise ConflictError(f"cannot spend a {group['status']} reservation group")
        revisions = dict(expected_revisions or {})
        member_ids = {item["reservation_id"] for item in group["members"]}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in group["members"]:
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
        updated: list[Record] = []
        for member in group["members"]:
            reservation = self._state.reservations[(self.company_id, member["reservation_id"])]
            budget = self._state.budgets[(self.company_id, reservation["budget_id"])]
            budget["reserved"] -= increment
            budget["spent"] += increment
            budget["revision"] += 1
            budget["updated_at"] = timestamp
            reservation["actual_amount"] = next_actual
            reservation["revision"] += 1
            reservation["updated_at"] = timestamp
            updated.append(reservation)
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
        """Resize an active reservation hold without changing applied spend."""

        self._check_open()
        group = await self.get_reservation_group(reservation_group_id)
        if group is None:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
        if group["status"] != "active":
            raise ConflictError(f"cannot resize a {group['status']} reservation group")
        next_amount = _positive_money(amount)
        applied = _reservation_applied_amount(group)
        if next_amount < applied:
            raise BudgetError("reservation hold cannot drop below already applied spend")
        current = _reservation_hold_amount(group)
        revisions = dict(expected_revisions or {})
        member_ids = {item["reservation_id"] for item in group["members"]}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in group["members"]:
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
        metadata_copy = None if metadata is None else copy.deepcopy(dict(metadata))
        if delta == 0 and metadata_copy is None:
            return group
        if delta > 0:
            for member in group["members"]:
                budget = self._state.budgets[(self.company_id, member["budget_id"])]
                self._assert_budget_usable(budget, timestamp)
                available = budget["limit"] - budget["spent"] - budget["reserved"]
                if delta > available:
                    raise BudgetError(
                        f"{member['ledger_role']} reservation exceeds available budget "
                        f"({available} {ledger_label(budget['unit'], budget['currency'])})"
                    )

        updated: list[Record] = []
        for member in group["members"]:
            reservation = self._state.reservations[(self.company_id, member["reservation_id"])]
            budget = self._state.budgets[(self.company_id, reservation["budget_id"])]
            if delta != 0:
                budget["reserved"] += delta
                budget["revision"] += 1
                budget["updated_at"] = timestamp
            reservation["amount"] = next_amount
            if metadata_copy is not None:
                reservation["metadata"] = copy.deepcopy(metadata_copy)
            reservation["revision"] += 1
            reservation["updated_at"] = timestamp
            updated.append(reservation)
        return _budget_group_record(updated)

    async def release_reservation(
        self,
        *,
        reservation_id: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        reservation_id = _required(reservation_id, "reservation_id")
        reservation_key = (self.company_id, reservation_id)
        reservation = self._state.reservations.get(reservation_key)
        if reservation is None:
            raise NotFoundError(f"reservation {reservation_id!r} does not exist")
        if reservation["reservation_group_id"] != reservation_id:
            raise ConflictError("group reservation members must be released through their group")
        expected_revision = _revision(expected_revision)
        if reservation["status"] == "released":
            return copy.deepcopy(reservation)
        if reservation["status"] != "active":
            raise ConflictError(f"cannot release a {reservation['status']} reservation")
        if expected_revision is not None and reservation["revision"] != expected_revision:
            raise ConflictError(
                "stale reservation revision: "
                f"expected {expected_revision}, current {reservation['revision']}"
            )

        timestamp = _utc(now)
        remaining = _reservation_remaining_hold(reservation)
        budget = self._state.budgets[(self.company_id, reservation["budget_id"])]
        budget["reserved"] -= remaining
        budget["revision"] += 1
        budget["updated_at"] = timestamp
        reservation["status"] = "released"
        reservation["revision"] += 1
        reservation["updated_at"] = timestamp
        return copy.deepcopy(reservation)

    async def release_reservation_group(
        self,
        *,
        reservation_group_id: str,
        expected_revisions: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        group = await self.get_reservation_group(reservation_group_id)
        if group is None:
            raise NotFoundError(f"reservation group {reservation_group_id!r} does not exist")
        if group["status"] == "released":
            return group
        if group["status"] != "active":
            raise ConflictError(f"cannot release a {group['status']} reservation group")
        revisions = dict(expected_revisions or {})
        member_ids = {item["reservation_id"] for item in group["members"]}
        if set(revisions) - member_ids:
            raise ValidationError("expected_revisions contains an unknown reservation")
        for member in group["members"]:
            expected = revisions.get(member["reservation_id"])
            if expected is not None:
                _revision(expected, required=True)
                if member["revision"] != expected:
                    raise ConflictError(
                        f"stale reservation revision: expected {expected}, "
                        f"current {member['revision']}"
                    )

        timestamp = _utc(now)
        remaining = _reservation_remaining_hold(group)
        updated: list[Record] = []
        for member in group["members"]:
            reservation = self._state.reservations[(self.company_id, member["reservation_id"])]
            budget = self._state.budgets[(self.company_id, reservation["budget_id"])]
            budget["reserved"] -= remaining
            budget["revision"] += 1
            budget["updated_at"] = timestamp
            reservation["status"] = "released"
            reservation["revision"] += 1
            reservation["updated_at"] = timestamp
            updated.append(reservation)
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
        self._check_open()
        work_item_id = _required(work_item_id, "work_item_id")
        agent_id = _required(agent_id, "agent_id")
        lease_id = _required(lease_id or str(uuid4()), "lease_id")
        token = _required(token or secrets.token_urlsafe(32), "token")
        timestamp = _utc(now)
        duration = _ttl(ttl)
        expected_revision = _revision(expected_revision)
        key = (self.company_id, work_item_id)
        existing = self._state.leases.get(key)
        if (
            existing is not None
            and existing["status"] == "active"
            and existing["expires_at"] > timestamp
        ):
            if (
                existing["lease_id"] == lease_id
                and existing["agent_id"] == agent_id
                and existing["agent_run_id"]
                == (str(agent_run_id) if agent_run_id is not None else None)
            ):
                return copy.deepcopy(existing)
            raise LeaseError(f"work item {work_item_id!r} already has an active lease")
        current_revision = existing["revision"] if existing is not None else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise ConflictError(
                f"stale lease revision: expected {expected_revision}, current {current_revision}"
            )
        record: Record = {
            "lease_id": lease_id,
            "company_id": self.company_id,
            "work_item_id": work_item_id,
            "agent_id": agent_id,
            "agent_run_id": str(agent_run_id) if agent_run_id is not None else None,
            "cell_id": str(cell_id) if cell_id is not None else None,
            "acquired_at": timestamp,
            "expires_at": timestamp + duration,
            "token": token,
            "status": "active",
            "revision": 0 if existing is None else current_revision + 1,
            "released_at": None,
            "updated_at": timestamp,
        }
        self._state.leases[key] = record
        return copy.deepcopy(record)

    async def get_lease(
        self,
        work_item_id: str,
        *,
        include_inactive: bool = False,
        now: datetime | None = None,
    ) -> Record | None:
        self._check_open()
        key = (self.company_id, _required(work_item_id, "work_item_id"))
        record = self._state.leases.get(key)
        if record is None:
            return None
        if not include_inactive:
            timestamp = _utc(now)
            if record["status"] != "active" or record["expires_at"] <= timestamp:
                return None
        return copy.deepcopy(record)

    async def renew_lease(
        self,
        *,
        work_item_id: str,
        token: str,
        ttl: timedelta | int | float,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        work_item_id = _required(work_item_id, "work_item_id")
        token = _required(token, "token")
        timestamp = _utc(now)
        expected_revision = _revision(expected_revision)
        key = (self.company_id, work_item_id)
        record = self._state.leases.get(key)
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
        record["expires_at"] = expires_at
        record["revision"] += 1
        record["updated_at"] = timestamp
        return copy.deepcopy(record)

    async def release_lease(
        self,
        *,
        work_item_id: str,
        token: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        work_item_id = _required(work_item_id, "work_item_id")
        token = _required(token, "token")
        timestamp = _utc(now)
        expected_revision = _revision(expected_revision)
        key = (self.company_id, work_item_id)
        record = self._state.leases.get(key)
        if record is None:
            raise NotFoundError(f"lease for work item {work_item_id!r} does not exist")
        if record["status"] == "released" and secrets.compare_digest(record["token"], token):
            return copy.deepcopy(record)
        self._assert_active_lease(record, token, timestamp)
        if expected_revision is not None and record["revision"] != expected_revision:
            raise ConflictError(
                f"stale lease revision: expected {expected_revision}, current {record['revision']}"
            )
        record["status"] = "released"
        record["released_at"] = timestamp
        record["revision"] += 1
        record["updated_at"] = timestamp
        return copy.deepcopy(record)

    async def expire_lease(
        self,
        *,
        work_item_id: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> Record:
        self._check_open()
        work_item_id = _required(work_item_id, "work_item_id")
        timestamp = _utc(now)
        expected_revision = _revision(expected_revision)
        key = (self.company_id, work_item_id)
        record = self._state.leases.get(key)
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
        record["status"] = "released"
        record["released_at"] = timestamp
        record["revision"] += 1
        record["updated_at"] = timestamp
        return copy.deepcopy(record)

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
        self._check_open()
        limit = _page(limit)
        if isinstance(after_position, bool) or not isinstance(after_position, int):
            raise ValidationError("after_position must be a non-negative integer")
        if after_position < 0:
            raise ValidationError("after_position must not be negative")
        records = [
            event
            for event in self._state.events
            if event["company_id"] == self.company_id
            and event["position"] > after_position
            and (aggregate_type is None or event["aggregate_type"] == aggregate_type)
            and (aggregate_id is None or event["aggregate_id"] == aggregate_id)
        ]
        return _copy_records(records[:limit])

    async def list_running_agent_runs(
        self,
        *,
        after_expires_at: datetime | None = None,
        after_agent_run_id: str | None = None,
        limit: int = 200,
    ) -> list[Record]:
        """Running runs ordered by (expires_at, agent_run_id), keyset paged."""

        self._check_open()
        limit = max(1, min(int(limit), 500))
        runs = sorted(
            (
                run
                for run in self._state.agent_runs.values()
                if run["company_id"] == self.company_id and run["status"] == "running"
            ),
            key=lambda run: (run["expires_at"], run["agent_run_id"]),
        )
        if after_expires_at is not None:
            cursor = (_utc(after_expires_at), after_agent_run_id or "")
            runs = [run for run in runs if (run["expires_at"], run["agent_run_id"]) > cursor]
        return [
            {
                "agent_run_id": run["agent_run_id"],
                "agent_instance_id": run["agent_instance_id"],
                "role": run["role"],
                "cell_id": run["cell_id"],
                "expires_at": run["expires_at"],
                "instance_status": "active",
            }
            for run in runs[:limit]
        ]

    async def end_cell_agent_runs(
        self,
        *,
        cell_id: str,
        termination_code: str,
        ended_at: datetime | None = None,
    ) -> list[str]:
        """End every running agent run bound to one cell; returns ended run ids."""

        self._check_open()
        cell_id = _required(cell_id, "cell_id")
        termination_code = _required(termination_code, "termination_code")
        timestamp = _utc(ended_at)
        ended: list[str] = []
        for run in self._state.agent_runs.values():
            if (
                run["company_id"] == self.company_id
                and run["cell_id"] == cell_id
                and run["status"] == "running"
            ):
                run["status"] = "revoked"
                run["termination_code"] = termination_code
                run["ended_at"] = max(timestamp, run["started_at"])
                run["revision"] += 1
                run["updated_at"] = max(timestamp, run["updated_at"])
                ended.append(run["agent_run_id"])
        return sorted(ended)

    async def list_audit_events(
        self,
        *,
        after_position: int = 0,
        limit: int = 1_000,
    ) -> list[Record]:
        self._check_open()
        limit = _page(limit)
        if isinstance(after_position, bool) or not isinstance(after_position, int):
            raise ValidationError("after_position must be a non-negative integer")
        if after_position < 0:
            raise ValidationError("after_position must not be negative")
        records = [
            event
            for event in self._state.audit_events
            if event["company_id"] == self.company_id and event["position"] > after_position
        ]
        return _copy_records(records[:limit])

    async def export(self) -> Record:
        """Return a deterministic, company-only snapshot suitable for JSON encoding."""

        self._check_open()
        company = self.company_id

        def by_company(record: Record) -> bool:
            return record["company_id"] == company

        projections = sorted(
            (value for value in self._state.projections.values() if by_company(value)),
            key=lambda value: (value["projection_type"], value["projection_id"]),
        )
        idempotency = sorted(
            (value for value in self._state.idempotency.values() if by_company(value)),
            key=lambda value: (value["namespace"], value["key"]),
        )
        budgets = sorted(
            (value for value in self._state.budgets.values() if by_company(value)),
            key=lambda value: value["budget_id"],
        )
        reservations = sorted(
            (value for value in self._state.reservations.values() if by_company(value)),
            key=lambda value: value["reservation_id"],
        )
        leases = sorted(
            (value for value in self._state.leases.values() if by_company(value)),
            key=lambda value: value["work_item_id"],
        )
        exported_leases = [
            {key: value for key, value in lease.items() if key != "token"} for lease in leases
        ]
        return {
            "company_id": company,
            "exported_at": utc_now(),
            "domain_events": _copy_records(
                [event for event in self._state.events if by_company(event)]
            ),
            "audit_events": _copy_records(
                [event for event in self._state.audit_events if by_company(event)]
            ),
            "projections": _copy_records(projections),
            "idempotency_records": _redact_export_secrets(idempotency),
            "budgets": _copy_records(budgets),
            "reservations": _copy_records(reservations),
            "work_leases": _copy_records(exported_leases),
        }


__all__ = [
    "MAX_PAGE_SIZE",
    "InMemoryStore",
    "Record",
    "RecordMapping",
    "Reducer",
    "Repository",
    "StoreTransaction",
    "TransactionContext",
    "TransactionalRepository",
    "utc_now",
]

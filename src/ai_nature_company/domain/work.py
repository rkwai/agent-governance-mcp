"""Canonical work-item state machine with separate runtime claim projection."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from ai_nature_company.errors import ConflictError, LeaseError, ValidationError
from ai_nature_company.models import (
    AgentBinding,
    AgentDefinition,
    AgentStatus,
    ApprovalRequirement,
    LeaseStatus,
    WorkItem,
    WorkItemState,
    WorkLease,
    WorkStatus,
)

MAX_LEASE_DURATION = timedelta(hours=24)


def _check_time(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValidationError("transition timestamp must be timezone-aware")


def _check_revision(current: int, expected: int) -> None:
    if current != expected:
        raise ConflictError(f"expected revision {expected}, found {current}")


def _replace_item(item: WorkItem, *, expected_revision: int, **updates: object) -> WorkItem:
    _check_revision(item.revision, expected_revision)
    values = item.model_dump(mode="python", round_trip=True)
    values.update(updates)
    values["revision"] = item.revision + 1
    return WorkItem.model_validate(values)


def _replace_state(state: WorkItemState, item: WorkItem, **updates: object) -> WorkItemState:
    values = state.model_dump(mode="python", round_trip=True)
    values.update(updates)
    values["item"] = item
    return WorkItemState.model_validate(values)


def _replace_lease(
    lease: WorkLease,
    *,
    expected_revision: int,
    at: datetime,
    **updates: object,
) -> WorkLease:
    _check_revision(lease.revision, expected_revision)
    _check_time(at)
    if at < lease.updated_at:
        raise ValidationError("transition timestamp cannot precede lease updated_at")
    values = lease.model_dump(mode="python", round_trip=True)
    values.update(updates)
    values["revision"] = lease.revision + 1
    values["updated_at"] = at
    return WorkLease.model_validate(values)


def _validate_duration(lease_for: timedelta) -> None:
    if lease_for <= timedelta(0):
        raise LeaseError("lease duration must be greater than zero")
    if lease_for > MAX_LEASE_DURATION:
        raise LeaseError("lease duration cannot exceed 24 hours")


def _validate_live_lease(
    state: WorkItemState,
    lease: WorkLease,
    *,
    principal_id: UUID,
    agent_run_id: UUID,
    lease_token: str,
    at: datetime,
) -> None:
    item = state.item
    if lease.work_item_id != item.work_item_id or state.lease_id != lease.lease_id:
        raise LeaseError("lease does not belong to this work item")
    if lease.company_id != item.company_id or lease.cell_id != item.cell_id:
        raise LeaseError("lease tenant boundary differs from work item")
    if lease.principal_id != principal_id:
        raise LeaseError("lease belongs to another principal")
    if lease.agent_run_id != agent_run_id:
        raise LeaseError("lease belongs to another agent run")
    if lease.token != lease_token:
        raise LeaseError("lease token does not match")
    if lease.status is not LeaseStatus.ACTIVE:
        raise LeaseError("lease is not active")
    if not lease.is_live_at(at):
        raise LeaseError("lease has expired")


def claim_work(
    state: WorkItemState,
    binding: AgentBinding,
    definition: AgentDefinition,
    *,
    effective_capabilities: frozenset[str],
    agent_run_id: UUID,
    satisfied_approvals: frozenset[ApprovalRequirement] = frozenset(),
    expected_revision: int,
    at: datetime,
    lease_for: timedelta,
    lease_id: UUID | None = None,
    token: str | None = None,
) -> tuple[WorkItemState, WorkLease]:
    """Atomically move open work to claimed for one verified binding and run."""

    item = state.item
    _check_revision(item.revision, expected_revision)
    _check_time(at)
    _validate_duration(lease_for)
    if item.status is not WorkStatus.OPEN:
        raise LeaseError(f"work is {item.status.value}, not open")
    if state.attempt >= item.max_attempts:
        raise LeaseError("work has reached its maximum attempts")
    if item.available_at is not None and at < item.available_at:
        raise LeaseError("work is not available yet")
    if item.deadline_at is not None and at >= item.deadline_at:
        raise LeaseError("work deadline has passed")
    if binding.status is not AgentStatus.ACTIVE:
        raise ValidationError("agent binding is not active")
    if binding.company_id != item.company_id:
        raise ValidationError("agent binding and work item belong to different company")
    if binding.cell_id is not None and binding.cell_id != item.cell_id:
        raise ValidationError("agent binding and work item belong to different cell")
    if (
        binding.definition_id != definition.definition_id
        or binding.definition_version != definition.version
    ):
        raise ValidationError("agent binding does not match definition version")
    if not effective_capabilities <= definition.declared_capabilities:
        raise ValidationError("effective capabilities exceed definition declaration")
    missing = item.eligible_capabilities - effective_capabilities
    if missing:
        raise ValidationError(
            f"principal lacks required capabilities: {', '.join(sorted(missing))}"
        )
    required_claim_approvals = item.approval_requirements - {ApprovalRequirement.NONE}
    if ApprovalRequirement.FOUNDER_BEFORE_CLAIM in required_claim_approvals and (
        ApprovalRequirement.FOUNDER_BEFORE_CLAIM not in satisfied_approvals
    ):
        raise ValidationError("founder approval is required before claim")

    resolved_lease_id = lease_id or uuid4()
    resolved_token = token or uuid4().hex
    claimed_item = _replace_item(
        item,
        expected_revision=expected_revision,
        status=WorkStatus.CLAIMED,
    )
    claimed_state = _replace_state(
        state,
        claimed_item,
        attempt=state.attempt + 1,
        lease_id=resolved_lease_id,
        failure_code=None,
        updated_at=at,
    )
    lease = WorkLease(
        lease_id=resolved_lease_id,
        work_item_id=item.work_item_id,
        company_id=item.company_id,
        cell_id=item.cell_id,
        principal_id=binding.principal_id,
        agent_run_id=agent_run_id,
        token=resolved_token,
        acquired_at=at,
        expires_at=at + lease_for,
        created_at=at,
        updated_at=at,
    )
    return claimed_state, lease


def renew_lease(
    lease: WorkLease,
    *,
    principal_id: UUID,
    agent_run_id: UUID,
    lease_token: str,
    expected_revision: int,
    at: datetime,
    lease_for: timedelta,
) -> WorkLease:
    _check_revision(lease.revision, expected_revision)
    _validate_duration(lease_for)
    if lease.principal_id != principal_id:
        raise LeaseError("lease belongs to another principal")
    if lease.agent_run_id != agent_run_id:
        raise LeaseError("lease belongs to another agent run")
    if lease.token != lease_token:
        raise LeaseError("lease token does not match")
    if lease.status is not LeaseStatus.ACTIVE:
        raise LeaseError("lease is not active")
    if not lease.is_live_at(at):
        raise LeaseError("lease has expired")
    expires_at = at + lease_for
    if expires_at <= lease.expires_at:
        raise LeaseError("renewal must extend the current lease expiration")
    return _replace_lease(
        lease,
        expected_revision=expected_revision,
        at=at,
        expires_at=expires_at,
    )


heartbeat_work = renew_lease


def complete_work(
    state: WorkItemState,
    lease: WorkLease,
    *,
    principal_id: UUID,
    agent_run_id: UUID,
    lease_token: str,
    evidence_ids: frozenset[UUID],
    actual_cost_minor: int,
    satisfied_approvals: frozenset[ApprovalRequirement] = frozenset(),
    expected_revision: int,
    expected_lease_revision: int,
    at: datetime,
) -> tuple[WorkItemState, WorkLease]:
    item = state.item
    _check_revision(item.revision, expected_revision)
    _check_revision(lease.revision, expected_lease_revision)
    if item.status is not WorkStatus.CLAIMED:
        raise ValidationError(f"work cannot transition from {item.status.value} to completed")
    _validate_live_lease(
        state,
        lease,
        principal_id=principal_id,
        agent_run_id=agent_run_id,
        lease_token=lease_token,
        at=at,
    )
    if not evidence_ids:
        raise ValidationError("completion requires result evidence")
    if isinstance(actual_cost_minor, bool) or actual_cost_minor < 0:
        raise ValidationError("actual_cost_minor must be a non-negative integer")
    if actual_cost_minor > item.budget.maximum_cost_minor:
        raise ValidationError("actual cost exceeds work budget")
    if (
        ApprovalRequirement.FOUNDER_BEFORE_COMPLETION in item.approval_requirements
        and ApprovalRequirement.FOUNDER_BEFORE_COMPLETION not in satisfied_approvals
    ):
        raise ValidationError("founder approval is required before completion")
    completed_item = _replace_item(
        item,
        expected_revision=expected_revision,
        status=WorkStatus.COMPLETED,
    )
    completed_state = _replace_state(
        state,
        completed_item,
        lease_id=None,
        result_evidence_ids=evidence_ids,
        actual_cost_minor=actual_cost_minor,
        failure_code=None,
        updated_at=at,
    )
    released_lease = _replace_lease(
        lease,
        expected_revision=expected_lease_revision,
        at=at,
        status=LeaseStatus.RELEASED,
        released_at=at,
    )
    return completed_state, released_lease


def fail_work(
    state: WorkItemState,
    lease: WorkLease,
    *,
    principal_id: UUID,
    agent_run_id: UUID,
    lease_token: str,
    failure_code: str,
    retryable: bool,
    expected_revision: int,
    expected_lease_revision: int,
    at: datetime,
) -> tuple[WorkItemState, WorkLease]:
    item = state.item
    _check_revision(item.revision, expected_revision)
    _check_revision(lease.revision, expected_lease_revision)
    if item.status is not WorkStatus.CLAIMED:
        raise ValidationError(f"work cannot fail from {item.status.value}")
    _validate_live_lease(
        state,
        lease,
        principal_id=principal_id,
        agent_run_id=agent_run_id,
        lease_token=lease_token,
        at=at,
    )
    target = (
        WorkStatus.OPEN if retryable and state.attempt < item.max_attempts else WorkStatus.FAILED
    )
    failed_item = _replace_item(item, expected_revision=expected_revision, status=target)
    failed_state = _replace_state(
        state,
        failed_item,
        lease_id=None,
        failure_code=failure_code if target is WorkStatus.FAILED else None,
        updated_at=at,
    )
    released_lease = _replace_lease(
        lease,
        expected_revision=expected_lease_revision,
        at=at,
        status=LeaseStatus.RELEASED,
        released_at=at,
    )
    return failed_state, released_lease


def release_lease(
    state: WorkItemState,
    lease: WorkLease,
    *,
    principal_id: UUID,
    agent_run_id: UUID,
    lease_token: str,
    expected_revision: int,
    expected_lease_revision: int,
    at: datetime,
) -> tuple[WorkItemState, WorkLease]:
    return fail_work(
        state,
        lease,
        principal_id=principal_id,
        agent_run_id=agent_run_id,
        lease_token=lease_token,
        failure_code="voluntary_release",
        retryable=True,
        expected_revision=expected_revision,
        expected_lease_revision=expected_lease_revision,
        at=at,
    )


def requeue_expired_work(
    state: WorkItemState,
    lease: WorkLease,
    *,
    expected_revision: int,
    expected_lease_revision: int,
    at: datetime,
) -> tuple[WorkItemState, WorkLease]:
    item = state.item
    _check_revision(item.revision, expected_revision)
    _check_revision(lease.revision, expected_lease_revision)
    _check_time(at)
    if item.status is not WorkStatus.CLAIMED:
        raise ValidationError(f"work cannot expire a lease from {item.status.value}")
    if state.lease_id != lease.lease_id or item.work_item_id != lease.work_item_id:
        raise LeaseError("lease does not belong to this work item")
    if lease.status is not LeaseStatus.ACTIVE:
        raise LeaseError("lease is not active")
    if at < lease.expires_at:
        raise LeaseError("lease has not expired")
    target = WorkStatus.FAILED if state.attempt >= item.max_attempts else WorkStatus.OPEN
    updated_item = _replace_item(item, expected_revision=expected_revision, status=target)
    updated_state = _replace_state(
        state,
        updated_item,
        lease_id=None,
        failure_code="lease_expired" if target is WorkStatus.FAILED else None,
        updated_at=at,
    )
    revoked_lease = _replace_lease(
        lease,
        expected_revision=expected_lease_revision,
        at=at,
        status=LeaseStatus.REVOKED,
        released_at=at,
    )
    return updated_state, revoked_lease


def cancel_work(
    state: WorkItemState,
    *,
    expected_revision: int,
    at: datetime,
    lease: WorkLease | None = None,
    expected_lease_revision: int | None = None,
) -> tuple[WorkItemState, WorkLease | None]:
    item = state.item
    _check_revision(item.revision, expected_revision)
    _check_time(at)
    if item.status not in {WorkStatus.OPEN, WorkStatus.CLAIMED}:
        raise ValidationError(f"work cannot cancel from {item.status.value}")
    if item.status is WorkStatus.CLAIMED:
        if lease is None or expected_lease_revision is None:
            raise LeaseError("claimed work cancellation requires its lease")
        if state.lease_id != lease.lease_id:
            raise LeaseError("lease does not belong to this work item")
        cancelled_lease: WorkLease | None = _replace_lease(
            lease,
            expected_revision=expected_lease_revision,
            at=at,
            status=LeaseStatus.REVOKED,
            released_at=at,
        )
    else:
        cancelled_lease = None
    cancelled_item = _replace_item(
        item,
        expected_revision=expected_revision,
        status=WorkStatus.CANCELLED,
    )
    cancelled_state = _replace_state(
        state,
        cancelled_item,
        lease_id=None,
        updated_at=at,
    )
    return cancelled_state, cancelled_lease


__all__ = [
    "cancel_work",
    "claim_work",
    "complete_work",
    "fail_work",
    "heartbeat_work",
    "release_lease",
    "renew_lease",
    "requeue_expired_work",
]

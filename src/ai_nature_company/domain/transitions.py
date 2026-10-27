"""Deterministic state transitions for canonical contracts and runtime projections."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel

from ai_nature_company.errors import BudgetError, ConflictError, ValidationError
from ai_nature_company.models import (
    Allocation,
    AllocationStatus,
    Budget,
    BudgetPolicy,
    BudgetReservation,
    BudgetReservationStatus,
    BudgetStatus,
    CellStatus,
    Company,
    CompanyStatus,
    Constitution,
    ConstitutionStatus,
    Experiment,
    ExperimentStatus,
    FitnessPolicy,
    LearningCandidate,
    LearningStatus,
    Offer,
    OfferCell,
    RiskPolicy,
    VersionedRecord,
)


def _check_revision(current: int, expected: int) -> None:
    if current != expected:
        raise ConflictError(f"expected revision {expected}, found {current}")


def _check_time(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValidationError("transition timestamp must be timezone-aware")


def _revised[RecordT: VersionedRecord](
    record: RecordT,
    *,
    expected_revision: int,
    at: datetime,
    **updates: object,
) -> RecordT:
    _check_revision(record.revision, expected_revision)
    _check_time(at)
    if at < record.updated_at:
        raise ValidationError("transition timestamp cannot precede current updated_at")
    values = record.model_dump(mode="python", round_trip=True)
    values.update(updates)
    values["revision"] = record.revision + 1
    values["updated_at"] = at
    return type(record).model_validate(values)


def _revised_contract[ModelT: BaseModel](
    record: ModelT,
    *,
    expected_revision: int,
    **updates: object,
) -> ModelT:
    current = getattr(record, "revision", None)
    if not isinstance(current, int):
        raise TypeError("canonical contract has no revision")
    _check_revision(current, expected_revision)
    values = record.model_dump(mode="python", round_trip=True)
    values.update(updates)
    values["revision"] = current + 1
    return type(record).model_validate(values)


def _replace_contract[ModelT: BaseModel](record: ModelT, **updates: object) -> ModelT:
    values = record.model_dump(mode="python", round_trip=True)
    values.update(updates)
    return type(record).model_validate(values)


def _require_transition(
    aggregate: str,
    current: object,
    target: object,
    allowed: Mapping[object, frozenset[object]],
) -> None:
    if target not in allowed.get(current, frozenset()):
        current_value = getattr(current, "value", str(current))
        target_value = getattr(target, "value", str(target))
        raise ValidationError(
            f"{aggregate} cannot transition from {current_value} to {target_value}"
        )


_COMPANY_TRANSITIONS: dict[object, frozenset[object]] = {
    CompanyStatus.ACTIVE: frozenset({CompanyStatus.PAUSED, CompanyStatus.DISSOLVED}),
    CompanyStatus.PAUSED: frozenset({CompanyStatus.ACTIVE, CompanyStatus.DISSOLVED}),
    CompanyStatus.DISSOLVED: frozenset(),
}


def transition_company(
    company: Company,
    target: CompanyStatus,
    *,
    expected_revision: int,
    at: datetime,
    founder_authorized: bool = False,
) -> Company:
    _require_transition("company", company.status, target, _COMPANY_TRANSITIONS)
    if target is CompanyStatus.DISSOLVED and not founder_authorized:
        raise ValidationError("company dissolution requires founder authority")
    return _revised(company, expected_revision=expected_revision, at=at, status=target)


def activate_constitution(
    constitution: Constitution,
    *,
    expected_version: int,
    at: datetime,
    founder_authorized: bool,
) -> Constitution:
    """Activate one canonical constitution; store CAS remains outside the wire model."""

    if constitution.version != expected_version:
        raise ConflictError(
            f"expected constitution version {expected_version}, found {constitution.version}"
        )
    _check_time(at)
    if not founder_authorized:
        raise ValidationError("constitution activation requires founder authority")
    if constitution.status is not ConstitutionStatus.DRAFT:
        raise ValidationError(
            f"constitution cannot transition from {constitution.status.value} to active"
        )
    return _replace_contract(
        constitution,
        status=ConstitutionStatus.ACTIVE,
        effective_at=at,
    )


def supersede_constitution(
    constitution: Constitution,
    *,
    expected_version: int,
    replacement_constitution_id: UUID,
    at: datetime,
    founder_authorized: bool,
) -> Constitution:
    if constitution.version != expected_version:
        raise ConflictError(
            f"expected constitution version {expected_version}, found {constitution.version}"
        )
    _check_time(at)
    if not founder_authorized:
        raise ValidationError("constitution supersession requires founder authority")
    if constitution.status is not ConstitutionStatus.ACTIVE:
        raise ValidationError(
            f"constitution cannot transition from {constitution.status.value} to superseded"
        )
    if replacement_constitution_id == constitution.constitution_id:
        raise ValidationError("replacement constitution must be a different version")
    return _replace_contract(
        constitution,
        status=ConstitutionStatus.SUPERSEDED,
        supersedes_constitution_id=replacement_constitution_id,
    )


def amend_constitution(
    constitution: Constitution,
    *,
    expected_version: int,
    at: datetime,
    founder_authorized: bool,
    created_by_principal_id: UUID,
    budget_policy: BudgetPolicy,
    mission: str | None = None,
    risk_policy: RiskPolicy | None = None,
    fitness_policy: FitnessPolicy | None = None,
    constitution_id: UUID | None = None,
) -> Constitution:
    """Activate the next constitution version. Same id plus a version bump is allowed."""

    if constitution.version != expected_version:
        raise ConflictError(
            f"expected constitution version {expected_version}, found {constitution.version}"
        )
    _check_time(at)
    if not founder_authorized:
        raise ValidationError("constitution amendment requires founder authority")
    if constitution.status is not ConstitutionStatus.ACTIVE:
        raise ValidationError(f"constitution cannot be amended from {constitution.status.value}")
    next_id = constitution_id or constitution.constitution_id
    return _replace_contract(
        constitution,
        constitution_id=next_id,
        version=constitution.version + 1,
        status=ConstitutionStatus.ACTIVE,
        mission=constitution.mission if mission is None else mission,
        risk_policy=constitution.risk_policy if risk_policy is None else risk_policy,
        budget_policy=budget_policy,
        fitness_policy=constitution.fitness_policy if fitness_policy is None else fitness_policy,
        created_at=at,
        created_by_principal_id=created_by_principal_id,
        effective_at=at,
        supersedes_constitution_id=None,
    )


_CELL_TRANSITIONS: dict[object, frozenset[object]] = {
    CellStatus.DRAFT: frozenset({CellStatus.READY}),
    CellStatus.READY: frozenset({CellStatus.ACTIVE}),
    CellStatus.ACTIVE: frozenset({CellStatus.PAUSED, CellStatus.SCALED, CellStatus.KILLED}),
    CellStatus.PAUSED: frozenset({CellStatus.ACTIVE, CellStatus.SCALED, CellStatus.KILLED}),
    CellStatus.SCALED: frozenset(),
    CellStatus.KILLED: frozenset(),
}


def transition_offer_cell(
    cell: OfferCell,
    target: CellStatus,
    *,
    expected_revision: int,
    at: datetime,
) -> OfferCell:
    _require_transition("offer cell", cell.state, target, _CELL_TRANSITIONS)
    updates: dict[str, object] = {"state": target}
    if target is CellStatus.READY:
        updates["ready_at"] = at
    if target is CellStatus.ACTIVE and cell.activated_at is None:
        updates["activated_at"] = at
    if target in {CellStatus.SCALED, CellStatus.KILLED}:
        updates["closed_at"] = at
    return _revised(cell, expected_revision=expected_revision, at=at, **updates)


_OFFER_MUTABLE_FIELDS = frozenset(
    {
        "target_segment",
        "urgent_problem",
        "promised_outcome",
        "proof",
        "price",
        "commercial_model",
        "acquisition_channel",
        "delivery_recipe",
        "time_to_value_days",
        "risk_reversal",
    }
)


def mutate_offer(
    parent: Offer,
    *,
    offer_version_id: UUID,
    registered_at: datetime,
    registered_by_principal_id: UUID,
    mutation_summary: str,
    changes: Mapping[str, object],
) -> Offer:
    """Create an immutable child version; the parent is never changed."""

    _check_time(registered_at)
    if offer_version_id == parent.offer_version_id:
        raise ValidationError("child offer_version_id must differ from parent")
    unknown = set(changes) - _OFFER_MUTABLE_FIELDS
    if unknown:
        raise ValidationError(f"offer mutation contains immutable fields: {sorted(unknown)}")
    if not changes:
        raise ValidationError("offer mutation must change at least one material field")
    values = parent.model_dump(mode="python", round_trip=True)
    values.update(changes)
    values.update(
        {
            "offer_version_id": offer_version_id,
            "version": parent.version + 1,
            "parent_offer_version_id": parent.offer_version_id,
            "mutation_summary": mutation_summary,
            "registered_at": registered_at,
            "registered_by_principal_id": registered_by_principal_id,
        }
    )
    return Offer.model_validate(values)


def transition_offer(*_: object, **__: object) -> Offer:
    """Reject the old lifecycle operation: offer changes always create a child version."""

    raise ValidationError("offers have no lifecycle transition; create an immutable child version")


_EXPERIMENT_TRANSITIONS: dict[object, frozenset[object]] = {
    ExperimentStatus.DRAFT: frozenset({ExperimentStatus.REGISTERED}),
    ExperimentStatus.REGISTERED: frozenset({ExperimentStatus.RUNNING, ExperimentStatus.CANCELLED}),
    ExperimentStatus.RUNNING: frozenset({ExperimentStatus.EVALUATING, ExperimentStatus.CANCELLED}),
    ExperimentStatus.EVALUATING: frozenset(
        {
            ExperimentStatus.PASSED,
            ExperimentStatus.FAILED,
            ExperimentStatus.INCONCLUSIVE,
        }
    ),
    ExperimentStatus.PASSED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.INCONCLUSIVE: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
}


def transition_experiment(
    experiment: Experiment,
    target: ExperimentStatus,
    *,
    expected_revision: int,
    at: datetime,
) -> Experiment:
    """Change only state and revision; registered protocol fields remain frozen."""

    _check_time(at)
    _require_transition("experiment", experiment.state, target, _EXPERIMENT_TRANSITIONS)
    if target is ExperimentStatus.RUNNING and not (
        experiment.window.starts_at <= at < experiment.window.ends_at
    ):
        raise ValidationError("experiment cannot start outside its registered window")
    return _revised_contract(
        experiment,
        expected_revision=expected_revision,
        state=target,
    )


def _amount_minor(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BudgetError("amount_minor must be a positive integer")
    return value


def _require_active_budget(budget: Budget, at: datetime) -> None:
    _check_time(at)
    if budget.status is not BudgetStatus.ACTIVE:
        raise BudgetError(f"budget is {budget.status.value}, not active")
    if not budget.starts_at <= at < budget.ends_at:
        raise BudgetError("budget is outside its active period")


def reserve_budget(
    budget: Budget,
    amount_minor: int,
    *,
    expected_revision: int,
    at: datetime,
) -> Budget:
    _check_revision(budget.revision, expected_revision)
    amount = _amount_minor(amount_minor)
    _require_active_budget(budget, at)
    if amount > budget.available_minor:
        raise BudgetError(f"amount {amount} exceeds available budget {budget.available_minor}")
    return _revised(
        budget,
        expected_revision=expected_revision,
        at=at,
        reserved_minor=budget.reserved_minor + amount,
    )


def release_budget_reservation(
    budget: Budget,
    amount_minor: int,
    *,
    expected_revision: int,
    at: datetime,
) -> Budget:
    _check_revision(budget.revision, expected_revision)
    amount = _amount_minor(amount_minor)
    if amount > budget.reserved_minor:
        raise BudgetError(f"cannot release {amount}; only {budget.reserved_minor} is reserved")
    status = BudgetStatus.ACTIVE if budget.status is BudgetStatus.EXHAUSTED else budget.status
    return _revised(
        budget,
        expected_revision=expected_revision,
        at=at,
        reserved_minor=budget.reserved_minor - amount,
        status=status,
    )


def record_budget_reservation(
    budget: Budget,
    *,
    amount_minor: int,
    purpose: str,
    idempotency_key: str,
    expected_revision: int,
    at: datetime,
    reservation_id: UUID | None = None,
    principal_id: UUID | None = None,
    experiment_id: UUID | None = None,
    work_item_id: UUID | None = None,
) -> tuple[Budget, BudgetReservation]:
    updated_budget = reserve_budget(
        budget,
        amount_minor,
        expected_revision=expected_revision,
        at=at,
    )
    reservation = BudgetReservation(
        reservation_id=reservation_id or uuid4(),
        budget_id=budget.budget_id,
        company_id=budget.company_id,
        amount_minor=amount_minor,
        unit=budget.unit,
        currency=budget.currency,
        purpose=purpose,
        idempotency_key=idempotency_key,
        principal_id=principal_id,
        experiment_id=experiment_id,
        work_item_id=work_item_id,
        reserved_at=at,
        created_at=at,
        updated_at=at,
    )
    return updated_budget, reservation


def _validate_reservation(budget: Budget, reservation: BudgetReservation) -> None:
    if reservation.budget_id != budget.budget_id:
        raise ValidationError("reservation references a different budget")
    if reservation.company_id != budget.company_id:
        raise ValidationError("reservation references a different company")
    if reservation.unit != budget.unit or reservation.currency != budget.currency:
        raise ValidationError("reservation unit of account differs from budget")


def consume_budget(
    budget: Budget,
    *,
    reservation: BudgetReservation,
    expected_budget_revision: int,
    expected_reservation_revision: int,
    at: datetime,
) -> tuple[Budget, BudgetReservation]:
    _check_revision(budget.revision, expected_budget_revision)
    _check_revision(reservation.revision, expected_reservation_revision)
    _check_time(at)
    _validate_reservation(budget, reservation)
    if budget.status is BudgetStatus.CLOSED:
        raise BudgetError("closed budget cannot settle spend")
    if reservation.status is not BudgetReservationStatus.RESERVED:
        raise ValidationError(
            f"reservation cannot transition from {reservation.status.value} to spent"
        )
    if reservation.amount_minor > budget.reserved_minor:
        raise BudgetError("reservation exceeds reserved budget")
    remaining = budget.reserved_minor - reservation.amount_minor
    spent = budget.spent_minor + reservation.amount_minor
    status = (
        BudgetStatus.EXHAUSTED if spent + remaining == budget.limit_minor else BudgetStatus.ACTIVE
    )
    updated_budget = _revised(
        budget,
        expected_revision=expected_budget_revision,
        at=at,
        spent_minor=spent,
        reserved_minor=remaining,
        status=status,
    )
    updated_reservation = _revised(
        reservation,
        expected_revision=expected_reservation_revision,
        at=at,
        status=BudgetReservationStatus.SPENT,
        spent_at=at,
    )
    return updated_budget, updated_reservation


def release_reservation(
    budget: Budget,
    reservation: BudgetReservation,
    *,
    expected_budget_revision: int,
    expected_reservation_revision: int,
    at: datetime,
) -> tuple[Budget, BudgetReservation]:
    _check_revision(budget.revision, expected_budget_revision)
    _check_revision(reservation.revision, expected_reservation_revision)
    _validate_reservation(budget, reservation)
    if reservation.status is not BudgetReservationStatus.RESERVED:
        raise ValidationError(
            f"reservation cannot transition from {reservation.status.value} to released"
        )
    updated_budget = release_budget_reservation(
        budget,
        reservation.amount_minor,
        expected_revision=expected_budget_revision,
        at=at,
    )
    updated_reservation = _revised(
        reservation,
        expected_revision=expected_reservation_revision,
        at=at,
        status=BudgetReservationStatus.RELEASED,
        released_at=at,
    )
    return updated_budget, updated_reservation


def cancel_reservation(
    budget: Budget,
    reservation: BudgetReservation,
    *,
    expected_budget_revision: int,
    expected_reservation_revision: int,
    at: datetime,
) -> tuple[Budget, BudgetReservation]:
    _check_revision(budget.revision, expected_budget_revision)
    _check_revision(reservation.revision, expected_reservation_revision)
    _validate_reservation(budget, reservation)
    if reservation.status is not BudgetReservationStatus.RESERVED:
        raise ValidationError(
            f"reservation cannot transition from {reservation.status.value} to cancelled"
        )
    updated_budget = release_budget_reservation(
        budget,
        reservation.amount_minor,
        expected_revision=expected_budget_revision,
        at=at,
    )
    updated_reservation = _revised(
        reservation,
        expected_revision=expected_reservation_revision,
        at=at,
        status=BudgetReservationStatus.CANCELLED,
        released_at=at,
    )
    return updated_budget, updated_reservation


def commit_allocation(
    allocation: Allocation,
    *,
    expected_revision: int,
    decided_by_principal_id: UUID,
    decided_by_run_id: UUID,
    at: datetime,
) -> Allocation:
    _check_time(at)
    if allocation.decision_state is not AllocationStatus.PROPOSED:
        raise ValidationError(
            f"allocation cannot transition from {allocation.decision_state.value} to committed"
        )
    return _revised_contract(
        allocation,
        expected_revision=expected_revision,
        decision_state=AllocationStatus.COMMITTED,
        decided_by_principal_id=decided_by_principal_id,
        decided_by_run_id=decided_by_run_id,
        decided_at=at,
    )


def reject_allocation(
    allocation: Allocation,
    *,
    expected_revision: int,
    decided_by_principal_id: UUID,
    decided_by_run_id: UUID,
    at: datetime,
) -> Allocation:
    _check_time(at)
    if allocation.decision_state is not AllocationStatus.PROPOSED:
        raise ValidationError(
            f"allocation cannot transition from {allocation.decision_state.value} to rejected"
        )
    return _revised_contract(
        allocation,
        expected_revision=expected_revision,
        decision_state=AllocationStatus.REJECTED,
        decided_by_principal_id=decided_by_principal_id,
        decided_by_run_id=decided_by_run_id,
        decided_at=at,
    )


_LEARNING_TRANSITIONS: dict[object, frozenset[object]] = {
    LearningStatus.CANDIDATE: frozenset({LearningStatus.TESTING, LearningStatus.REJECTED}),
    LearningStatus.TESTING: frozenset({LearningStatus.VALIDATED, LearningStatus.REJECTED}),
    LearningStatus.VALIDATED: frozenset({LearningStatus.PROMOTED, LearningStatus.REJECTED}),
    LearningStatus.PROMOTED: frozenset({LearningStatus.ROLLED_BACK}),
    LearningStatus.REJECTED: frozenset(),
    LearningStatus.ROLLED_BACK: frozenset(),
}


def transition_learning(
    learning: LearningCandidate,
    target: LearningStatus,
    *,
    expected_revision: int,
    at: datetime,
    validated_by_principal_id: UUID | None = None,
) -> LearningCandidate:
    _require_transition("learning", learning.status, target, _LEARNING_TRANSITIONS)
    updates: dict[str, object] = {"status": target}
    if target is LearningStatus.VALIDATED:
        if validated_by_principal_id is None:
            raise ValidationError("validation requires a validating principal")
        updates["validated_by_principal_id"] = validated_by_principal_id
    return _revised(
        learning,
        expected_revision=expected_revision,
        at=at,
        **updates,
    )


# Transitional function aliases for callers migrating from the pre-schema reservation name.
record_allocation = record_budget_reservation
release_allocation = release_reservation
cancel_allocation = cancel_reservation


__all__ = [
    "activate_constitution",
    "amend_constitution",
    "cancel_allocation",
    "cancel_reservation",
    "commit_allocation",
    "consume_budget",
    "mutate_offer",
    "record_allocation",
    "record_budget_reservation",
    "reject_allocation",
    "release_allocation",
    "release_budget_reservation",
    "release_reservation",
    "reserve_budget",
    "supersede_constitution",
    "transition_company",
    "transition_experiment",
    "transition_learning",
    "transition_offer",
    "transition_offer_cell",
]

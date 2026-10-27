"""Tests for canonical and runtime state transitions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from ai_nature_company.domain.transitions import (
    activate_constitution,
    amend_constitution,
    commit_allocation,
    consume_budget,
    mutate_offer,
    record_budget_reservation,
    reject_allocation,
    release_reservation,
    reserve_budget,
    transition_experiment,
    transition_learning,
    transition_offer_cell,
)
from ai_nature_company.errors import BudgetError, ConflictError, ValidationError
from ai_nature_company.models import (
    Allocation,
    AllocationAction,
    AllocationRanking,
    AllocationStage,
    AllocationStatus,
    AllocationTarget,
    AllocationTargetKind,
    AuthorityPolicy,
    Budget,
    BudgetPolicy,
    BudgetReservationStatus,
    BudgetScope,
    CellStatus,
    CommercialModel,
    Constitution,
    ConstitutionStatus,
    Experiment,
    ExperimentStatus,
    ExperimentWindow,
    FitnessPolicy,
    GateName,
    GateResult,
    LearningCandidate,
    LearningStatus,
    Metric,
    MetricDirection,
    Money,
    Offer,
    OfferCell,
    RankingFormula,
    RankingInputSnapshot,
    RiskPolicy,
    StopRule,
    StopRuleEffect,
    Threshold,
    ThresholdOperator,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_offer() -> Offer:
    return Offer(
        offer_version_id=uuid4(),
        offer_id=uuid4(),
        company_id=uuid4(),
        cell_id=uuid4(),
        version=1,
        parent_offer_version_id=None,
        target_segment="Independent accounting firms",
        urgent_problem="Month-end exceptions delay close.",
        promised_outcome="Resolve exceptions within two days.",
        proof=("Verified simulated delivery",),
        price=Money(currency="USD", amount_minor=250_000),
        commercial_model=CommercialModel.FIXED_FEE,
        acquisition_channel="Founder-led email",
        delivery_recipe=("Ingest exceptions", "Return reconciliation"),
        time_to_value_days=2,
        risk_reversal="No fee if acceptance criteria fail.",
        registered_at=NOW,
        registered_by_principal_id=uuid4(),
    )


def make_experiment() -> Experiment:
    return Experiment(
        experiment_id=uuid4(),
        company_id=uuid4(),
        cell_id=uuid4(),
        offer_version_id=uuid4(),
        revision=1,
        state=ExperimentStatus.DRAFT,
        hypothesis="A concrete offer produces qualified replies.",
        baseline="No offer is sent.",
        treatment="Send one registered offer.",
        target_population="Independent accounting firms",
        metrics=(
            Metric(
                metric_id="qualified_replies",
                name="Qualified replies",
                unit="count",
                direction=MetricDirection.INCREASE,
            ),
        ),
        success_thresholds=(
            Threshold(
                metric_id="qualified_replies",
                operator=ThresholdOperator.GTE,
                value=Decimal("3"),
            ),
        ),
        failure_thresholds=(
            Threshold(
                metric_id="qualified_replies",
                operator=ThresholdOperator.EQ,
                value=Decimal("0"),
            ),
        ),
        stop_rules=(
            StopRule(
                rule_id="complaint",
                condition="Any verified complaint is recorded.",
                effect=StopRuleEffect.CANCEL_EXPERIMENT,
            ),
        ),
        window=ExperimentWindow(starts_at=NOW, ends_at=NOW + timedelta(days=7)),
        budget=Money(currency="USD", amount_minor=5_000),
        permitted_actions=frozenset({"crm.message.create"}),
        executor_principal_ids=frozenset({uuid4()}),
        evaluator_principal_id=uuid4(),
        registered_by_principal_id=uuid4(),
        registered_at=NOW,
    )


def make_budget() -> Budget:
    return Budget(
        company_id=uuid4(),
        scope=BudgetScope.COMPANY,
        limit_minor=10_000,
        starts_at=NOW,
        ends_at=NOW + timedelta(days=30),
        created_at=NOW,
        updated_at=NOW,
    )


def make_constitution() -> Constitution:
    return Constitution(
        constitution_id=uuid4(),
        company_id=uuid4(),
        version=1,
        status=ConstitutionStatus.DRAFT,
        mission="Create verified customer value profitably.",
        authority_policy=AuthorityPolicy(
            founder_principal_ids=frozenset({uuid4()}),
            irreversible_actions=frozenset({"constitution.activate"}),
            human_approval_actions=frozenset({"company.dissolve"}),
        ),
        risk_policy=RiskPolicy(
            prohibited_actions=frozenset({"message.deceptive_send"}),
            pricing_floor_minor=100,
            max_external_messages_per_experiment=10,
            regulated_market_entry_requires_founder=True,
        ),
        budget_policy=BudgetPolicy(
            currency="USD",
            company_limit_minor=100_000,
            cell_limit_minor=50_000,
            experiment_limit_minor=10_000,
            single_action_limit_minor=5_000,
            exploration_share_bps=2_000,
        ),
        fitness_policy=FitnessPolicy(
            required_gates=frozenset(GateName),
            minimum_evidence_count=1,
            minimum_customer_outcome_bps=7_000,
            minimum_delivery_quality_bps=8_000,
            allocation_cadence_days=7,
        ),
        created_at=NOW,
        created_by_principal_id=uuid4(),
    )


def make_allocation() -> Allocation:
    profit, confidence, repeatability, capital, days = 100_000, 8_000, 7_000, 50_000, 45
    return Allocation(
        allocation_id=uuid4(),
        company_id=uuid4(),
        revision=1,
        decision_state=AllocationStatus.PROPOSED,
        action=AllocationAction.FUND,
        target=AllocationTarget(kind=AllocationTargetKind.CELL, id=uuid4(), revision=3),
        stage=AllocationStage.EXPLOITATION,
        gate_results=tuple(
            GateResult(gate=gate, passed=True, evidence_ids=frozenset({uuid4()}))
            for gate in GateName
        ),
        ranking=AllocationRanking(
            formula=RankingFormula.EXPLOITATION_V1,
            score_numerator=profit * confidence * repeatability * 30,
            score_denominator=max(capital, 1) * max(days, 30) * 10_000 * 10_000,
            input_snapshot=RankingInputSnapshot(
                expected_12m_contribution_profit_minor=profit,
                evidence_confidence_bps=confidence,
                expected_repeatability_bps=repeatability,
                capital_at_risk_minor=capital,
                time_to_cash_days=days,
            ),
        ),
        amount=Money(currency="USD", amount_minor=10_000),
        constitution_id=uuid4(),
        constitution_version=1,
        input_digest="0" * 64,
        rationale_summary="Fund the highest eligible exploitation candidate.",
        proposed_by_principal_id=uuid4(),
        proposed_by_run_id=uuid4(),
        proposed_at=NOW,
        decided_by_principal_id=None,
        decided_by_run_id=None,
        decided_at=None,
    )


def test_offer_mutation_creates_child_without_changing_parent() -> None:
    parent = make_offer()
    child = mutate_offer(
        parent,
        offer_version_id=uuid4(),
        registered_at=NOW + timedelta(minutes=1),
        registered_by_principal_id=uuid4(),
        mutation_summary="Raised price after verified demand.",
        changes={"price": Money(currency="USD", amount_minor=300_000)},
    )

    assert parent.version == 1
    assert parent.price.amount_minor == 250_000
    assert child.version == 2
    assert child.parent_offer_version_id == parent.offer_version_id
    assert child.offer_id == parent.offer_id


def test_offer_mutation_rejects_identity_changes() -> None:
    with pytest.raises(ValidationError, match="immutable fields"):
        mutate_offer(
            make_offer(),
            offer_version_id=uuid4(),
            registered_at=NOW,
            registered_by_principal_id=uuid4(),
            mutation_summary="Invalid identity change.",
            changes={"company_id": uuid4()},
        )


def test_experiment_lifecycle_changes_only_state_and_revision() -> None:
    draft = make_experiment()
    registered = transition_experiment(
        draft, ExperimentStatus.REGISTERED, expected_revision=1, at=NOW
    )
    running = transition_experiment(
        registered, ExperimentStatus.RUNNING, expected_revision=2, at=NOW
    )
    evaluating = transition_experiment(
        running,
        ExperimentStatus.EVALUATING,
        expected_revision=3,
        at=NOW + timedelta(days=1),
    )
    passed = transition_experiment(
        evaluating,
        ExperimentStatus.PASSED,
        expected_revision=4,
        at=NOW + timedelta(days=1),
    )

    assert passed.revision == 5
    original_material = draft.model_dump(exclude={"state", "revision"})
    final_material = passed.model_dump(exclude={"state", "revision"})
    assert final_material == original_material


def test_experiment_cannot_skip_evaluation_or_use_stale_revision() -> None:
    draft = make_experiment()
    with pytest.raises(ConflictError, match="expected revision"):
        transition_experiment(draft, ExperimentStatus.REGISTERED, expected_revision=9, at=NOW)
    registered = transition_experiment(
        draft, ExperimentStatus.REGISTERED, expected_revision=1, at=NOW
    )
    running = transition_experiment(
        registered, ExperimentStatus.RUNNING, expected_revision=2, at=NOW
    )
    with pytest.raises(ValidationError, match="cannot transition"):
        transition_experiment(running, ExperimentStatus.PASSED, expected_revision=3, at=NOW)


def test_budget_reservation_consumption_is_exact_in_minor_units() -> None:
    budget, reservation = record_budget_reservation(
        make_budget(),
        amount_minor=2_500,
        purpose="Run acquisition experiment",
        idempotency_key="reservation-1",
        expected_revision=0,
        at=NOW,
        experiment_id=uuid4(),
    )
    spent_budget, spent_reservation = consume_budget(
        budget,
        reservation=reservation,
        expected_budget_revision=1,
        expected_reservation_revision=0,
        at=NOW + timedelta(seconds=1),
    )

    assert spent_budget.spent_minor == 2_500
    assert spent_budget.reserved_minor == 0
    assert spent_reservation.status is BudgetReservationStatus.SPENT


def test_budget_rejects_floats_and_over_reservation() -> None:
    with pytest.raises(BudgetError, match="positive integer"):
        reserve_budget(make_budget(), 1.5, expected_revision=0, at=NOW)  # type: ignore[arg-type]
    with pytest.raises(BudgetError, match="available"):
        reserve_budget(make_budget(), 10_001, expected_revision=0, at=NOW)


def test_releasing_reservation_returns_budget() -> None:
    budget, reservation = record_budget_reservation(
        make_budget(),
        amount_minor=2_000,
        purpose="Experiment",
        idempotency_key="reservation-2",
        expected_revision=0,
        at=NOW,
    )
    released_budget, released = release_reservation(
        budget,
        reservation,
        expected_budget_revision=1,
        expected_reservation_revision=0,
        at=NOW + timedelta(seconds=1),
    )
    assert released_budget.reserved_minor == 0
    assert released.status is BudgetReservationStatus.RELEASED


def test_constitution_activation_requires_founder_authority() -> None:
    constitution = make_constitution()
    with pytest.raises(ValidationError, match="founder authority"):
        activate_constitution(
            constitution,
            expected_version=1,
            at=NOW,
            founder_authorized=False,
        )
    active = activate_constitution(
        constitution,
        expected_version=1,
        at=NOW,
        founder_authorized=True,
    )
    assert active.status is ConstitutionStatus.ACTIVE
    assert active.effective_at == NOW


def test_constitution_amendment_requires_founder_and_bumps_version() -> None:
    draft = make_constitution()
    active = activate_constitution(
        draft,
        expected_version=1,
        at=NOW,
        founder_authorized=True,
    )
    founder = active.created_by_principal_id
    next_policy = BudgetPolicy(
        unit="tries",
        company_limit_minor=20,
        cell_limit_minor=10,
        experiment_limit_minor=4,
        single_action_limit_minor=4,
        exploration_share_bps=2_000,
    )
    with pytest.raises(ValidationError, match="founder authority"):
        amend_constitution(
            active,
            expected_version=1,
            at=NOW,
            founder_authorized=False,
            created_by_principal_id=founder,
            budget_policy=next_policy,
        )
    with pytest.raises(ConflictError, match="constitution version"):
        amend_constitution(
            active,
            expected_version=2,
            at=NOW,
            founder_authorized=True,
            created_by_principal_id=founder,
            budget_policy=next_policy,
        )
    amended = amend_constitution(
        active,
        expected_version=1,
        at=NOW + timedelta(seconds=1),
        founder_authorized=True,
        created_by_principal_id=founder,
        budget_policy=next_policy,
        mission="Operate bounded try-budgeted offer cells.",
    )
    assert amended.constitution_id == active.constitution_id
    assert amended.version == 2
    assert amended.status is ConstitutionStatus.ACTIVE
    assert amended.budget_policy.unit.value == "tries"
    assert amended.mission == "Operate bounded try-budgeted offer cells."
    assert amended.fitness_policy.rule_version == "fitness-v1"
    assert amended.authority_policy == active.authority_policy


def test_offer_cell_lifecycle_does_not_skip_ready() -> None:
    cell = OfferCell(
        company_id=uuid4(),
        name="Offer cell",
        objective="Find a paid offer.",
        owner_principal_id=uuid4(),
        budget_id=uuid4(),
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ValidationError, match="cannot transition"):
        transition_offer_cell(cell, CellStatus.ACTIVE, expected_revision=0, at=NOW)


def test_allocation_commit_is_versioned_and_reject_is_terminal() -> None:
    proposal = make_allocation()
    committed = commit_allocation(
        proposal,
        expected_revision=1,
        decided_by_principal_id=uuid4(),
        decided_by_run_id=uuid4(),
        at=NOW + timedelta(minutes=1),
    )
    assert committed.decision_state is AllocationStatus.COMMITTED
    assert committed.revision == 2
    with pytest.raises(ValidationError, match="cannot transition"):
        reject_allocation(
            committed,
            expected_revision=2,
            decided_by_principal_id=uuid4(),
            decided_by_run_id=uuid4(),
            at=NOW + timedelta(minutes=2),
        )


def test_learning_lifecycle_requires_independent_validator() -> None:
    proposer = uuid4()
    candidate = LearningCandidate(
        company_id=uuid4(),
        cell_id=uuid4(),
        source_experiment_id=uuid4(),
        source_evaluation_id=uuid4(),
        statement="Shorter proof-led messages produce more qualified replies.",
        confidence_bps=7_500,
        proposed_by_principal_id=proposer,
        evidence_ids=frozenset({uuid4()}),
        created_at=NOW,
        updated_at=NOW,
    )
    testing = transition_learning(candidate, LearningStatus.TESTING, expected_revision=0, at=NOW)
    with pytest.raises(Exception, match="validator must differ"):
        transition_learning(
            testing,
            LearningStatus.VALIDATED,
            expected_revision=1,
            at=NOW,
            validated_by_principal_id=proposer,
        )

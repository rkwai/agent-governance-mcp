"""Contract tests for canonical wire models and separate runtime records."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from ai_nature_company.models import (
    AgentConstraints,
    AgentDefinition,
    AgentRole,
    Allocation,
    AuthorityPolicy,
    Budget,
    BudgetPolicy,
    BudgetScope,
    CausalDistance,
    CellStatus,
    CommercialModel,
    Company,
    Constitution,
    ConstitutionStatus,
    CriterionResult,
    Evaluation,
    EvaluationVerdict,
    EvidenceEnvelope,
    EvidenceLineage,
    EvidenceObservation,
    EvidenceRelation,
    EvidenceType,
    Experiment,
    ExperimentStatus,
    ExperimentWindow,
    FitnessPolicy,
    GateName,
    IndependenceAttestation,
    IndependenceBasis,
    IOContract,
    Metric,
    MetricDirection,
    Money,
    Offer,
    OfferCell,
    ProvenanceQuality,
    RiskPolicy,
    StopRule,
    StopRuleEffect,
    Threshold,
    ThresholdOperator,
    WorkItem,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SCHEMA_DIR = Path(__file__).parents[2] / "schemas"


def make_experiment(*, evaluator_id=None, executor_ids=None) -> Experiment:
    executors = frozenset(executor_ids or {uuid4()})
    return Experiment(
        experiment_id=uuid4(),
        company_id=uuid4(),
        cell_id=uuid4(),
        offer_version_id=uuid4(),
        revision=1,
        state=ExperimentStatus.DRAFT,
        hypothesis="A concrete offer will produce qualified replies.",
        baseline="No offer is sent.",
        treatment="Send the registered offer once.",
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
                rule_id="customer_complaint",
                condition="Any verified customer complaint is recorded.",
                effect=StopRuleEffect.CANCEL_EXPERIMENT,
            ),
        ),
        window=ExperimentWindow(starts_at=NOW, ends_at=NOW + timedelta(days=7)),
        budget=Money(currency="USD", amount_minor=5_000),
        permitted_actions=frozenset({"crm.message.create"}),
        executor_principal_ids=executors,
        evaluator_principal_id=evaluator_id or uuid4(),
        registered_by_principal_id=uuid4(),
        registered_at=NOW,
    )


def make_constitution(*, status=ConstitutionStatus.DRAFT) -> Constitution:
    return Constitution(
        constitution_id=uuid4(),
        company_id=uuid4(),
        version=1,
        status=status,
        mission="Create verified customer value and risk-adjusted contribution profit.",
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
            minimum_evidence_count=2,
            minimum_customer_outcome_bps=7_000,
            minimum_delivery_quality_bps=8_000,
            allocation_cadence_days=7,
        ),
        created_at=NOW,
        created_by_principal_id=uuid4(),
        effective_at=NOW if status is ConstitutionStatus.ACTIVE else None,
    )


@pytest.mark.parametrize(
    ("schema_name", "model"),
    [
        ("agent-definition-v1.json", AgentDefinition),
        ("allocation-v1.json", Allocation),
        ("company-constitution-v1.json", Constitution),
        ("evaluation-v1.json", Evaluation),
        ("evidence-v1.json", EvidenceEnvelope),
        ("experiment-v1.json", Experiment),
        ("offer-v1.json", Offer),
        ("work-item-v1.json", WorkItem),
    ],
)
def test_canonical_model_top_level_fields_match_json_schema(schema_name: str, model) -> None:
    schema = json.loads((SCHEMA_DIR / schema_name).read_text())
    assert set(model.model_fields) == set(schema["properties"])


def test_agent_definition_is_configuration_not_runtime_authority() -> None:
    definition = AgentDefinition(
        definition_id=uuid4(),
        version=1,
        name="market-specialist",
        purpose="Execute bounded market experiments.",
        instructions=("Claim work before acting.",),
        declared_capabilities=frozenset({"work.item.claim"}),
        compatible_roles=frozenset({AgentRole.SPECIALIST}),
        constraints=AgentConstraints(
            may_commit_allocation=False,
            requires_active_lease_for_work=True,
        ),
        input_contract=IOContract(schema_ref="schemas/work-item-v1.json"),
        output_contract=IOContract(schema_ref="schemas/evidence-v1.json"),
    )

    assert "company_id" not in type(definition).model_fields
    assert "role" not in type(definition).model_fields
    with pytest.raises(PydanticValidationError, match="Extra inputs"):
        AgentDefinition.model_validate({**definition.model_dump(), "caller_claimed_admin": True})


def test_offer_is_immutable_and_enforces_parent_lineage() -> None:
    offer = Offer(
        offer_version_id=uuid4(),
        offer_id=uuid4(),
        company_id=uuid4(),
        cell_id=uuid4(),
        version=1,
        parent_offer_version_id=None,
        target_segment="Independent accounting firms",
        urgent_problem="Month-end exceptions delay close.",
        promised_outcome="Resolve exceptions within two business days.",
        proof=("A simulated delivery receipt",),
        price=Money(currency="USD", amount_minor=250_000),
        commercial_model=CommercialModel.FIXED_FEE,
        acquisition_channel="Founder-led email",
        delivery_recipe=("Ingest exception export", "Return verified reconciliation"),
        time_to_value_days=2,
        risk_reversal="No fee if the acceptance criteria fail.",
        registered_at=NOW,
        registered_by_principal_id=uuid4(),
    )

    with pytest.raises(PydanticValidationError, match="frozen"):
        offer.price = Money(currency="USD", amount_minor=1)  # type: ignore[misc]
    with pytest.raises(PydanticValidationError, match="at least 2"):
        Offer.model_validate(
            {
                **offer.model_dump(),
                "parent_offer_version_id": uuid4(),
                "mutation_summary": "Changed price",
            }
        )


def test_experiment_enforces_metric_references_and_evaluator_independence() -> None:
    executor_id = uuid4()
    with pytest.raises(PydanticValidationError, match="evaluator principal"):
        make_experiment(evaluator_id=executor_id, executor_ids={executor_id})

    experiment = make_experiment()
    invalid_threshold = experiment.model_dump()
    invalid_threshold["success_thresholds"][0]["metric_id"] = "unknown_metric"
    with pytest.raises(PydanticValidationError, match="unknown metric"):
        Experiment.model_validate(invalid_threshold)


def test_evidence_is_canonical_tamper_checkable_and_lineage_safe() -> None:
    evidence_id = uuid4()
    values = {
        "evidence_id": evidence_id,
        "company_id": uuid4(),
        "cell_id": uuid4(),
        "offer_version_id": uuid4(),
        "experiment_id": uuid4(),
        "type": EvidenceType.REPLY,
        "source_system": "crm",
        "source_reference": "reply-42",
        "observed_at": NOW,
        "recorded_at": NOW + timedelta(seconds=1),
        "strength": Decimal("1"),
        "confidence_bps": 9_000,
        "provenance_quality": ProvenanceQuality.AUTHORITATIVE,
        "causal_distance": CausalDistance.DIRECT,
        "half_life_seconds": 86_400,
        "observation": EvidenceObservation(
            value="positive", unit="classification", summary="Prospect requested a meeting."
        ),
        "lineage": EvidenceLineage(relation=EvidenceRelation.ORIGINAL, related_evidence_id=None),
        "recorded_by_principal_id": uuid4(),
        "agent_run_id": uuid4(),
    }
    evidence = EvidenceEnvelope(**values)
    duplicate = EvidenceEnvelope(**values)
    assert evidence.content_digest == duplicate.content_digest

    with pytest.raises(PydanticValidationError, match="cannot reference itself"):
        EvidenceEnvelope.model_validate(
            {
                **evidence.model_dump(),
                "lineage": {
                    "relation": "correction",
                    "related_evidence_id": evidence_id,
                },
            }
        )


def test_budget_runtime_projection_cannot_overcommit() -> None:
    with pytest.raises(PydanticValidationError, match="exceed budget limit"):
        Budget(
            company_id=uuid4(),
            scope=BudgetScope.COMPANY,
            limit_minor=10_000,
            spent_minor=8_000,
            reserved_minor=2_001,
            starts_at=NOW,
            ends_at=NOW + timedelta(days=30),
            created_at=NOW,
            updated_at=NOW,
        )


def test_company_constitution_and_offer_cell_are_separate_records() -> None:
    constitution = make_constitution()
    company = Company(
        company_id=constitution.company_id,
        name="AI Nature Company",
        founder_principal_id=next(iter(constitution.authority_policy.founder_principal_ids)),
        active_constitution_id=constitution.constitution_id,
        created_at=NOW,
        updated_at=NOW,
    )
    cell = OfferCell(
        company_id=company.company_id,
        name="Close acceleration",
        objective="Find a repeatable paid offer.",
        owner_principal_id=uuid4(),
        budget_id=uuid4(),
        created_at=NOW,
        updated_at=NOW,
    )

    assert constitution.status is ConstitutionStatus.DRAFT
    assert cell.state is CellStatus.DRAFT


def test_evaluation_requires_independence_and_evidence_for_pass() -> None:
    executor_id = uuid4()
    with pytest.raises(PydanticValidationError, match="cannot be an experiment executor"):
        Evaluation(
            evaluation_id=uuid4(),
            company_id=uuid4(),
            experiment_id=uuid4(),
            experiment_revision=4,
            evaluator_principal_id=executor_id,
            evaluator_run_id=uuid4(),
            executor_principal_ids=frozenset({executor_id}),
            independence_attestation=IndependenceAttestation(
                basis=IndependenceBasis.SEPARATE_PRINCIPAL
            ),
            criterion_results=(
                CriterionResult(
                    criterion_id="qualified_replies",
                    outcome=EvaluationVerdict.PASSED,
                    observed_value=4,
                    evidence_ids=frozenset({uuid4()}),
                ),
            ),
            result=EvaluationVerdict.PASSED,
            summary="Target exceeded.",
            evaluated_at=NOW,
        )

    with pytest.raises(PydanticValidationError, match="requires evidence"):
        CriterionResult(
            criterion_id="qualified_replies",
            outcome=EvaluationVerdict.PASSED,
            observed_value=0,
            evidence_ids=frozenset(),
        )

    zero_fail = CriterionResult(
        criterion_id="qualified_replies",
        outcome=EvaluationVerdict.FAILED,
        observed_value=0,
        evidence_ids=frozenset(),
    )
    assert zero_fail.evidence_ids == frozenset()
    assert zero_fail.observed_value == 0

    with pytest.raises(PydanticValidationError, match="observed_value 0"):
        CriterionResult(
            criterion_id="qualified_replies",
            outcome=EvaluationVerdict.FAILED,
            observed_value=1,
            evidence_ids=frozenset(),
        )

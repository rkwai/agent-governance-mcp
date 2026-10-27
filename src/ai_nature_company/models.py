"""Canonical Phase 1 contracts plus separate runtime projection records.

The eight public protocol models in this module mirror the checked-in JSON schemas.
Runtime identity, leases, budgets, and projections are deliberately separate so their
fields cannot leak into or weaken the wire contracts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    field_validator,
    model_validator,
)

from .ledger_units import TIME_SCALE

SCHEMA_VERSION = 1
MAX_EVENT_PAYLOAD_BYTES = 32_768


def utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


AwareDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
NonEmptyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
]
ShortStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)]
CurrencyCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z]{3}$"),
]
CapabilityName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"),
]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
AgentName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
IdempotencyKey = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]
BasisPoints = Annotated[int, Field(ge=0, le=10_000)]
JsonScalar = str | int | float | bool | None


def _decimal_as_json_number(value: Decimal) -> int | float:
    if not value.is_finite():
        raise ValueError("JSON number must be finite")
    if value == value.to_integral_value():
        return int(value)
    return float(value)


JsonNumberDecimal = Annotated[
    Decimal,
    PlainSerializer(_decimal_as_json_number, return_type=int | float, when_used="json"),
]


class FrozenContract(BaseModel):
    """Strict immutable base for canonical values and runtime records."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class VersionedRecord(FrozenContract):
    """Runtime aggregate snapshot; not itself a canonical wire schema."""

    schema_version: Literal[1] = SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_record_times(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


# Shared finite vocabularies


class AgentRole(StrEnum):
    FOUNDER = "founder"
    ALLOCATOR = "allocator"
    CELL_OWNER = "cell_owner"
    SPECIALIST = "specialist"
    EVALUATOR = "evaluator"
    IMMUNE = "immune"
    OBSERVER = "observer"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompanyStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISSOLVED = "dissolved"


class ConstitutionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class CellStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    SCALED = "scaled"
    KILLED = "killed"


class CommercialModel(StrEnum):
    FIXED_FEE = "fixed_fee"
    SUBSCRIPTION = "subscription"
    USAGE = "usage"
    OUTCOME_BASED = "outcome_based"
    HYBRID = "hybrid"


class OfferStatus(StrEnum):
    """Legacy lifecycle vocabulary retained for imports; offers themselves are versioned facts."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"
    CANCELLED = "cancelled"


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    REGISTERED = "registered"
    RUNNING = "running"
    EVALUATING = "evaluating"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    CANCELLED = "cancelled"


class MetricDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    TARGET = "target"


class ThresholdOperator(StrEnum):
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    LTE = "lte"
    LT = "lt"


class StopRuleEffect(StrEnum):
    PAUSE_EXTERNAL_ACTIONS = "pause_external_actions"
    CANCEL_EXPERIMENT = "cancel_experiment"
    REQUIRE_FOUNDER_REVIEW = "require_founder_review"


class EvidenceType(StrEnum):
    REPLY = "reply"
    MEETING = "meeting"
    PROPOSAL = "proposal"
    COMMITMENT = "commitment"
    CASH = "cash"
    USAGE = "usage"
    OUTCOME = "outcome"
    COST = "cost"
    REFUND = "refund"
    COMPLAINT = "complaint"
    CHURN = "churn"
    DELIVERY_OVERRUN = "delivery_overrun"
    MANUAL_EXCEPTION = "manual_exception"
    SECURITY_INCIDENT = "security_incident"
    POLICY_VIOLATION = "policy_violation"
    OTHER = "other"


class ProvenanceQuality(StrEnum):
    AUTHORITATIVE = "authoritative"
    VERIFIED = "verified"
    ATTESTED = "attested"
    INFERRED = "inferred"


class CausalDistance(StrEnum):
    DIRECT = "direct"
    NEAR = "near"
    INTERMEDIATE = "intermediate"
    PROXY = "proxy"


class EvidenceRelation(StrEnum):
    ORIGINAL = "original"
    CORRECTION = "correction"
    SUPERSESSION = "supersession"
    RETRACTION = "retraction"


class EvaluationVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class IndependenceBasis(StrEnum):
    SEPARATE_PRINCIPAL = "separate_principal"
    HUMAN_APPROVAL = "human_approval"
    SEPARATE_PRINCIPAL_AND_HUMAN = "separate_principal_and_human"


class WorkStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalRequirement(StrEnum):
    NONE = "none"
    FOUNDER_BEFORE_CLAIM = "founder_before_claim"
    FOUNDER_BEFORE_EXTERNAL_ACTION = "founder_before_external_action"
    FOUNDER_BEFORE_COMPLETION = "founder_before_completion"


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"


class UnitOfAccount(StrEnum):
    """Company-wide ledger unit. One budget row holds exactly one unit."""

    MONEY = "money"
    TIME = "time"
    CYCLES = "cycles"
    TRIES = "tries"


class BudgetScope(StrEnum):
    COMPANY = "company"
    CELL = "cell"
    AGENT = "agent"
    EXPERIMENT = "experiment"


class BudgetStatus(StrEnum):
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    CLOSED = "closed"


class BudgetReservationStatus(StrEnum):
    RESERVED = "reserved"
    SPENT = "spent"
    RELEASED = "released"
    CANCELLED = "cancelled"


class AllocationStatus(StrEnum):
    PROPOSED = "proposed"
    COMMITTED = "committed"
    REJECTED = "rejected"


class AllocationAction(StrEnum):
    FUND = "fund"
    MAINTAIN = "maintain"
    CLONE = "clone"
    MUTATE = "mutate"
    PAUSE = "pause"
    KILL = "kill"


class AllocationTargetKind(StrEnum):
    CELL = "cell"
    EXPERIMENT = "experiment"


class AllocationStage(StrEnum):
    EXPLORATION = "exploration"
    EXPLOITATION = "exploitation"
    CONTAINMENT = "containment"


class GateName(StrEnum):
    LEGALITY_AND_POLICY = "legality_and_policy"
    TRUTHFULNESS_AND_CLAIM_SUPPORT = "truthfulness_and_claim_support"
    CUSTOMER_OUTCOME_FLOOR = "customer_outcome_floor"
    SECURITY_AND_PRIVACY = "security_and_privacy"
    BUDGET_AND_CASH = "budget_and_cash"
    DELIVERY_QUALITY_FLOOR = "delivery_quality_floor"
    DATA_SUFFICIENCY_AND_INDEPENDENCE = "data_sufficiency_and_independence"


class GateDecision(StrEnum):
    PASS = "pass"
    BLOCK = "block"
    REQUIRE_FOUNDER = "require_founder"


class RankingFormula(StrEnum):
    EXPLOITATION_V1 = "exploitation-v1"
    EXPLORATION_V1 = "exploration-v1"
    NOT_RANKED = "not-ranked"


class LearningStatus(StrEnum):
    CANDIDATE = "candidate"
    TESTING = "testing"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class AggregateType(StrEnum):
    COMPANY = "company"
    CONSTITUTION = "constitution"
    AGENT = "agent"
    AGENT_BINDING = "agent_binding"
    AGENT_RUN = "agent_run"
    OFFER_CELL = "offer_cell"
    OFFER = "offer"
    EXPERIMENT = "experiment"
    EVIDENCE = "evidence"
    EVALUATION = "evaluation"
    LEARNING = "learning"
    BUDGET = "budget"
    BUDGET_RESERVATION = "budget_reservation"
    ALLOCATION = "allocation"
    WORK_ITEM = "work_item"
    WORK_LEASE = "work_lease"
    POLICY = "policy"


# agent-definition-v1.json


class IOContract(FrozenContract):
    media_type: Literal["application/json"] = "application/json"
    schema_ref: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class AgentConstraints(FrozenContract):
    may_self_evaluate: Literal[False] = False
    may_commit_allocation: bool
    may_change_constitution: Literal[False] = False
    requires_active_lease_for_work: bool


class AgentDefinition(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    definition_id: UUID
    version: int = Field(ge=1)
    name: AgentName
    purpose: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    instructions: tuple[Annotated[str, StringConstraints(min_length=1, max_length=1_000)], ...] = (
        Field(min_length=1, max_length=50)
    )
    declared_capabilities: frozenset[CapabilityName] = Field(min_length=1)
    compatible_roles: frozenset[AgentRole] = Field(min_length=1)
    constraints: AgentConstraints
    input_contract: IOContract
    output_contract: IOContract
    supersedes_definition_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    description: Annotated[str, StringConstraints(max_length=2_000)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.declared_capabilities)


# company-constitution-v1.json


class AuthorityPolicy(FrozenContract):
    founder_principal_ids: frozenset[UUID] = Field(min_length=1)
    irreversible_actions: frozenset[CapabilityName] = Field(min_length=1)
    human_approval_actions: frozenset[CapabilityName] = Field(min_length=1)


class RiskPolicy(FrozenContract):
    prohibited_actions: frozenset[CapabilityName] = Field(min_length=1)
    pricing_floor_minor: int = Field(ge=0)
    max_external_messages_per_experiment: int = Field(ge=0)
    regulated_market_entry_requires_founder: Literal[True] = True


class BudgetPolicy(FrozenContract):
    """Company budget limits in exactly one unit of account.

    Omitted ``unit`` is money so existing USD constitutions still load.
    ``*_minor`` is the canonical integer of that unit: ISO minor units for
    money, whole seconds for time, and whole counts for cycles or tries.
    """

    unit: UnitOfAccount = Field(
        default=UnitOfAccount.MONEY,
        exclude_if=lambda value: value is UnitOfAccount.MONEY,
    )
    currency: CurrencyCode | None = Field(default=None, exclude_if=lambda value: value is None)
    company_limit_minor: int = Field(ge=0)
    cell_limit_minor: int = Field(ge=0)
    experiment_limit_minor: int = Field(ge=0)
    single_action_limit_minor: int = Field(ge=0)
    exploration_share_bps: BasisPoints

    @model_validator(mode="after")
    def validate_limit_hierarchy(self) -> Self:
        if self.unit is UnitOfAccount.MONEY:
            if self.currency is None:
                raise ValueError("money budget policy requires an ISO 4217 currency")
        elif self.currency is not None:
            raise ValueError("currency is only valid when unit is money")
        if self.cell_limit_minor > self.company_limit_minor:
            raise ValueError("cell limit cannot exceed company limit")
        if self.experiment_limit_minor > self.cell_limit_minor:
            raise ValueError("experiment limit cannot exceed cell limit")
        if self.single_action_limit_minor > self.experiment_limit_minor:
            raise ValueError("single-action limit cannot exceed experiment limit")
        return self


class FitnessPolicy(FrozenContract):
    rule_version: Literal["fitness-v1"] = "fitness-v1"
    required_gates: frozenset[GateName] = Field(min_length=7, max_length=7)
    minimum_evidence_count: int = Field(ge=1)
    minimum_customer_outcome_bps: BasisPoints
    minimum_delivery_quality_bps: BasisPoints
    allocation_cadence_days: int = Field(ge=1, le=365)

    @model_validator(mode="after")
    def require_complete_gate_set(self) -> Self:
        if self.required_gates != frozenset(GateName):
            raise ValueError("required_gates must contain the complete fitness-v1 gate set")
        return self


class Constitution(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    constitution_id: UUID
    company_id: UUID
    version: int = Field(ge=1)
    status: ConstitutionStatus
    mission: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    authority_policy: AuthorityPolicy
    risk_policy: RiskPolicy
    budget_policy: BudgetPolicy
    fitness_policy: FitnessPolicy
    created_at: AwareDatetime
    created_by_principal_id: UUID
    effective_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    supersedes_constitution_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_effective_state(self) -> Self:
        if self.status is ConstitutionStatus.ACTIVE and self.effective_at is None:
            raise ValueError("effective_at is required for an active constitution")
        if self.status is ConstitutionStatus.SUPERSEDED and self.supersedes_constitution_id is None:
            raise ValueError("supersedes_constitution_id is required for a superseded constitution")
        return self


# offer-v1.json


class Money(FrozenContract):
    """Commercial price in ISO 4217 minor units. Not a constraint-ledger quantity."""

    currency: CurrencyCode
    amount_minor: int = Field(ge=0)


class BudgetAmount(FrozenContract):
    """Integer quantity in one constraint-ledger unit of account.

    Existing ``{"currency": "USD", "amount_minor": N}`` payloads remain money.
    Time quantities are whole seconds. Cycles and tries are whole counts.
    """

    unit: UnitOfAccount = Field(
        default=UnitOfAccount.MONEY,
        exclude_if=lambda value: value is UnitOfAccount.MONEY,
    )
    currency: CurrencyCode | None = Field(default=None, exclude_if=lambda value: value is None)
    amount_minor: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_unit_fields(self) -> Self:
        if self.unit is UnitOfAccount.MONEY:
            if self.currency is None:
                raise ValueError("money quantities require an ISO 4217 currency")
        elif self.currency is not None:
            raise ValueError("currency is only valid for money quantities")
        return self


class Offer(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    offer_version_id: UUID
    offer_id: UUID
    company_id: UUID
    cell_id: UUID
    version: int = Field(ge=1)
    parent_offer_version_id: UUID | None
    target_segment: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    urgent_problem: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    promised_outcome: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    proof: tuple[Annotated[str, StringConstraints(min_length=1, max_length=1_000)], ...] = Field(
        min_length=1, max_length=25
    )
    price: Money
    commercial_model: CommercialModel
    acquisition_channel: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    delivery_recipe: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=1_000)], ...
    ] = Field(min_length=1, max_length=50)
    time_to_value_days: int = Field(ge=0, le=3_650)
    risk_reversal: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    mutation_summary: Annotated[str, StringConstraints(min_length=1, max_length=1_000)] | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )
    registered_at: AwareDatetime
    registered_by_principal_id: UUID

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        if self.parent_offer_version_id is None:
            if self.version != 1:
                raise ValueError("an original offer must have version 1")
            if self.mutation_summary is not None:
                raise ValueError("an original offer cannot have a mutation_summary")
        elif self.mutation_summary is None:
            raise ValueError("a mutated offer requires mutation_summary")
        elif self.version < 2:
            raise ValueError("a child offer version must be at least 2")
        if self.parent_offer_version_id == self.offer_version_id:
            raise ValueError("an offer version cannot parent itself")
        return self

    @property
    def audience(self) -> str:
        return self.target_segment

    @property
    def promise(self) -> str:
        return self.promised_outcome


OfferVersion = Offer


# experiment-v1.json


class Metric(FrozenContract):
    metric_id: Identifier
    name: ShortStr
    unit: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    direction: MetricDirection


class Threshold(FrozenContract):
    metric_id: Identifier
    operator: ThresholdOperator
    value: JsonNumberDecimal


class StopRule(FrozenContract):
    rule_id: Identifier
    condition: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    effect: StopRuleEffect


class ExperimentWindow(FrozenContract):
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("window ends_at must be later than starts_at")
        return self


class Experiment(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    experiment_id: UUID
    company_id: UUID
    cell_id: UUID
    offer_version_id: UUID
    revision: int = Field(ge=1)
    state: ExperimentStatus
    hypothesis: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    baseline: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    treatment: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    target_population: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    metrics: tuple[Metric, ...] = Field(min_length=1, max_length=25)
    success_thresholds: tuple[Threshold, ...] = Field(min_length=1)
    failure_thresholds: tuple[Threshold, ...] = Field(min_length=1)
    stop_rules: tuple[StopRule, ...] = Field(min_length=1, max_length=25)
    window: ExperimentWindow
    budget: BudgetAmount
    permitted_actions: frozenset[CapabilityName] = Field(min_length=1)
    executor_principal_ids: frozenset[UUID] = Field(min_length=1)
    evaluator_principal_id: UUID
    registered_by_principal_id: UUID
    registered_at: AwareDatetime

    @field_validator("budget", mode="before")
    @classmethod
    def coerce_money_budget(cls, value: Any) -> Any:
        if isinstance(value, Money):
            return BudgetAmount(
                unit=UnitOfAccount.MONEY,
                currency=value.currency,
                amount_minor=value.amount_minor,
            )
        return value

    @model_validator(mode="after")
    def validate_metric_references(self) -> Self:
        if self.evaluator_principal_id in self.executor_principal_ids:
            raise ValueError("evaluator principal cannot be an experiment executor")
        metric_ids = [metric.metric_id for metric in self.metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("metric_id values must be unique")
        known = set(metric_ids)
        for threshold in (*self.success_thresholds, *self.failure_thresholds):
            if threshold.metric_id not in known:
                raise ValueError(f"threshold references unknown metric {threshold.metric_id}")
        rule_ids = [rule.rule_id for rule in self.stop_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("stop rule IDs must be unique")
        return self

    @property
    def status(self) -> ExperimentStatus:
        return self.state

    @property
    def metric(self) -> str:
        return self.metrics[0].metric_id

    @property
    def target(self) -> Decimal:
        return self.success_thresholds[0].value

    @property
    def budget_amount_minor(self) -> int:
        return self.budget.amount_minor


# evidence-v1.json


class EvidenceObservation(FrozenContract):
    value: JsonScalar
    unit: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class EvidenceLineage(FrozenContract):
    relation: EvidenceRelation
    related_evidence_id: UUID | None

    @model_validator(mode="after")
    def validate_relation(self) -> Self:
        if self.relation is EvidenceRelation.ORIGINAL and self.related_evidence_id is not None:
            raise ValueError("original evidence cannot reference related evidence")
        if self.relation is not EvidenceRelation.ORIGINAL and self.related_evidence_id is None:
            raise ValueError("non-original evidence must reference related evidence")
        return self


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonicalize(child) for key, child in sorted(value.items())}
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_canonicalize(child) for child in value]
        if isinstance(value, (set, frozenset)):
            return sorted(normalized, key=lambda child: json.dumps(child, sort_keys=True))
        return normalized
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


class EvidenceEnvelope(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    evidence_id: UUID
    company_id: UUID
    cell_id: UUID
    offer_version_id: UUID | None
    experiment_id: UUID | None
    type: EvidenceType
    source_system: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    source_reference: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    observed_at: AwareDatetime
    recorded_at: AwareDatetime
    strength: JsonNumberDecimal = Field(ge=0)
    confidence_bps: BasisPoints
    provenance_quality: ProvenanceQuality
    causal_distance: CausalDistance
    half_life_seconds: int = Field(ge=1)
    observation: EvidenceObservation
    lineage: EvidenceLineage
    recorded_by_principal_id: UUID
    agent_run_id: UUID
    external_action_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.recorded_at < self.observed_at:
            raise ValueError("recorded_at cannot precede observed_at")
        if self.lineage.related_evidence_id == self.evidence_id:
            raise ValueError("evidence lineage cannot reference itself")
        return self

    @property
    def content_digest(self) -> str:
        canonical = json.dumps(
            _canonicalize(self.model_dump(mode="python")),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


# Vocabulary-only compatibility aliases; they introduce no alternative fields.
EvidenceKind = EvidenceType
EvidenceSource = ProvenanceQuality
EvidenceVerification = ProvenanceQuality


# evaluation-v1.json


class IndependenceAttestation(FrozenContract):
    is_independent: Literal[True] = True
    checked_by_substrate: Literal[True] = True
    basis: IndependenceBasis


def _is_zero_observed(value: JsonScalar) -> bool:
    """Return True when a criterion observed_value is an exact zero count."""

    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        try:
            return Decimal(value) == 0
        except Exception:
            return False
    return False


class CriterionResult(FrozenContract):
    criterion_id: Identifier
    outcome: EvaluationVerdict
    observed_value: JsonScalar
    evidence_ids: frozenset[UUID]

    @model_validator(mode="after")
    def validate_evidence_requirement(self) -> Self:
        if self.outcome is EvaluationVerdict.PASSED and not self.evidence_ids:
            raise ValueError("passed criterion requires evidence")
        if (
            self.outcome is EvaluationVerdict.FAILED
            and not self.evidence_ids
            and not _is_zero_observed(self.observed_value)
        ):
            raise ValueError("empty-evidence failed criterion requires observed_value 0")
        return self


class Evaluation(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    evaluation_id: UUID
    company_id: UUID
    experiment_id: UUID
    experiment_revision: int = Field(ge=1)
    evaluator_principal_id: UUID
    evaluator_run_id: UUID
    executor_principal_ids: frozenset[UUID] = Field(min_length=1)
    independence_attestation: IndependenceAttestation
    criterion_results: tuple[CriterionResult, ...] = Field(min_length=1, max_length=50)
    result: EvaluationVerdict
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4_000)]
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_independence_and_result(self) -> Self:
        if self.evaluator_principal_id in self.executor_principal_ids:
            raise ValueError("evaluator principal cannot be an experiment executor")
        outcomes = {criterion.outcome for criterion in self.criterion_results}
        if self.result is EvaluationVerdict.PASSED and outcomes != {EvaluationVerdict.PASSED}:
            raise ValueError("passed evaluation requires every criterion to pass")
        if (
            self.result is EvaluationVerdict.INCONCLUSIVE
            and EvaluationVerdict.INCONCLUSIVE not in outcomes
        ):
            raise ValueError("inconclusive evaluation requires an inconclusive criterion")
        return self

    @property
    def verdict(self) -> EvaluationVerdict:
        return self.result

    @property
    def evaluator_agent_id(self) -> UUID:
        return self.evaluator_principal_id


# work-item-v1.json


class OutcomeContract(FrozenContract):
    outcome: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    acceptance_criteria: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=1_000)], ...
    ] = Field(min_length=1, max_length=25)
    result_schema_ref: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class WorkBudget(FrozenContract):
    unit: UnitOfAccount = Field(
        default=UnitOfAccount.MONEY,
        exclude_if=lambda value: value is UnitOfAccount.MONEY,
    )
    currency: CurrencyCode | None = Field(default=None, exclude_if=lambda value: value is None)
    maximum_cost_minor: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_unit_fields(self) -> Self:
        if self.unit is UnitOfAccount.MONEY:
            if self.currency is None:
                raise ValueError("money work budgets require an ISO 4217 currency")
        elif self.currency is not None:
            raise ValueError("currency is only valid for money work budgets")
        return self


class ResourceReference(FrozenContract):
    kind: Identifier
    id: UUID
    revision: int = Field(ge=1)


class WorkItem(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    work_item_id: UUID
    company_id: UUID
    cell_id: UUID | None
    experiment_id: UUID | None
    revision: int = Field(ge=1)
    status: WorkStatus
    kind: Identifier
    outcome_contract: OutcomeContract
    eligible_capabilities: frozenset[CapabilityName] = Field(min_length=1)
    budget: WorkBudget
    approval_requirements: frozenset[ApprovalRequirement]
    input_references: tuple[ResourceReference, ...] = Field(max_length=100)
    max_attempts: int = Field(ge=1, le=100)
    created_by_principal_id: UUID
    created_at: AwareDatetime
    available_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    deadline_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    claimant_agent_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    lease_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    lease_expires_at: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    last_heartbeat_at: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    cancellation_history: tuple[dict[str, Any], ...] = Field(
        default_factory=tuple,
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        available = self.available_at or self.created_at
        if self.deadline_at is not None and self.deadline_at <= available:
            raise ValueError("deadline_at must be later than available_at")
        approvals = self.approval_requirements
        if not approvals:
            raise ValueError("approval_requirements cannot be empty")
        if ApprovalRequirement.NONE in approvals and approvals != {ApprovalRequirement.NONE}:
            raise ValueError("none cannot be combined with founder approval requirements")
        return self

    @property
    def required_capabilities(self) -> frozenset[str]:
        return frozenset(self.eligible_capabilities)


# allocation-v1.json


class AllocationTarget(FrozenContract):
    kind: AllocationTargetKind
    id: UUID
    revision: int = Field(ge=1)


class GateResult(FrozenContract):
    gate: GateName
    passed: bool
    evidence_ids: frozenset[UUID]


class RankingInputSnapshot(FrozenContract):
    expected_12m_contribution_profit_minor: int | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    evidence_confidence_bps: BasisPoints | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    expected_repeatability_bps: BasisPoints | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    capital_at_risk_minor: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    time_to_cash_days: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    expected_decision_value_minor: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    resolution_probability_bps: BasisPoints | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    experiment_cost_minor: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    experiment_duration_days: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )


class AllocationRanking(FrozenContract):
    formula: RankingFormula
    score_numerator: int
    score_denominator: int = Field(ge=1)
    input_snapshot: RankingInputSnapshot


class Allocation(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    allocation_id: UUID
    company_id: UUID
    revision: int = Field(ge=1)
    decision_state: AllocationStatus
    action: AllocationAction
    target: AllocationTarget
    stage: AllocationStage
    gate_results: tuple[GateResult, ...] = Field(min_length=7, max_length=7)
    ranking: AllocationRanking
    amount: BudgetAmount | None
    constitution_id: UUID
    constitution_version: int = Field(ge=1)
    rule_version: Literal["fitness-v1"] = "fitness-v1"
    input_digest: Digest
    rationale_summary: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    proposed_by_principal_id: UUID
    proposed_by_run_id: UUID
    proposed_at: AwareDatetime
    decided_by_principal_id: UUID | None
    decided_by_run_id: UUID | None
    decided_at: AwareDatetime | None

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_money_amount(cls, value: Any) -> Any:
        if isinstance(value, Money):
            return BudgetAmount(
                unit=UnitOfAccount.MONEY,
                currency=value.currency,
                amount_minor=value.amount_minor,
            )
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        gates = [result.gate for result in self.gate_results]
        if set(gates) != set(GateName) or len(gates) != len(set(gates)):
            raise ValueError("gate_results must contain each fitness-v1 gate exactly once")
        decided = self.decision_state is not AllocationStatus.PROPOSED
        required_decision_fields = (self.decided_by_principal_id, self.decided_at)
        all_decision_fields = (*required_decision_fields, self.decided_by_run_id)
        if decided and any(value is None for value in required_decision_fields):
            raise ValueError("terminal allocation requires deciding principal and time")
        if not decided and any(value is not None for value in all_decision_fields):
            raise ValueError("proposed allocation cannot contain decision attribution")
        if (
            self.stage is AllocationStage.CONTAINMENT
            and self.ranking.formula is not RankingFormula.NOT_RANKED
        ):
            raise ValueError("containment allocation must use not-ranked")
        containment_action = self.action in {AllocationAction.PAUSE, AllocationAction.KILL}
        if (self.stage is AllocationStage.CONTAINMENT) != containment_action:
            raise ValueError("containment stage is required exactly for pause or kill")
        if self.action is AllocationAction.FUND:
            if self.amount is None or self.amount.amount_minor < 1:
                raise ValueError("fund allocation requires a positive amount")
        elif self.amount is not None:
            raise ValueError("non-fund allocation must have null amount")

        snapshot = self.ranking.input_snapshot
        exploitation_fields = (
            snapshot.expected_12m_contribution_profit_minor,
            snapshot.evidence_confidence_bps,
            snapshot.expected_repeatability_bps,
            snapshot.capital_at_risk_minor,
            snapshot.time_to_cash_days,
        )
        exploration_fields = (
            snapshot.expected_decision_value_minor,
            snapshot.resolution_probability_bps,
            snapshot.experiment_cost_minor,
            snapshot.experiment_duration_days,
        )
        if self.stage is AllocationStage.EXPLOITATION:
            if self.ranking.formula is not RankingFormula.EXPLOITATION_V1:
                raise ValueError("exploitation stage requires exploitation-v1 ranking")
            if any(value is None for value in exploitation_fields) or any(
                value is not None for value in exploration_fields
            ):
                raise ValueError("exploitation ranking input field set is invalid")
            if self.decision_state is AllocationStatus.COMMITTED and not all(
                result.passed for result in self.gate_results
            ):
                raise ValueError("committed exploitation requires every gate to pass")
            profit, confidence, repeatability, capital, days = exploitation_fields
            assert all(value is not None for value in exploitation_fields)
            expected_numerator = int(profit) * int(confidence) * int(repeatability) * 30
            expected_denominator = max(int(capital), 1) * max(int(days), 30) * 10_000 * 10_000
            if (
                self.ranking.score_numerator != expected_numerator
                or self.ranking.score_denominator != expected_denominator
            ):
                raise ValueError("exploitation score fraction does not match fitness-v1")
        elif self.stage is AllocationStage.EXPLORATION:
            if self.ranking.formula is not RankingFormula.EXPLORATION_V1:
                raise ValueError("exploration stage requires exploration-v1 ranking")
            if any(value is None for value in exploration_fields) or any(
                value is not None for value in exploitation_fields
            ):
                raise ValueError("exploration ranking input field set is invalid")
            value, probability, cost, duration = exploration_fields
            assert all(item is not None for item in exploration_fields)
            expected_numerator = int(value) * int(probability)
            expected_denominator = max(int(cost), 1) * max(int(duration), 1) * 10_000
            if (
                self.ranking.score_numerator != expected_numerator
                or self.ranking.score_denominator != expected_denominator
            ):
                raise ValueError("exploration score fraction does not match fitness-v1")
        elif any(value is not None for value in (*exploitation_fields, *exploration_fields)):
            raise ValueError("containment ranking must not carry score inputs")
        elif self.ranking.score_numerator != 0 or self.ranking.score_denominator != 1:
            raise ValueError("not-ranked containment score must be 0/1")
        return self

    @property
    def status(self) -> AllocationStatus:
        return self.decision_state


# Runtime-only identity and aggregate projections


class AgentBinding(VersionedRecord):
    binding_id: UUID = Field(default_factory=uuid4)
    company_id: UUID
    principal_id: UUID
    definition_id: UUID
    definition_version: int = Field(ge=1)
    cell_id: UUID | None = None
    status: AgentStatus = AgentStatus.ACTIVE


class AgentRun(VersionedRecord):
    run_id: UUID = Field(default_factory=uuid4)
    company_id: UUID
    principal_id: UUID
    binding_id: UUID
    definition_id: UUID
    definition_version: int = Field(ge=1)
    status: AgentRunStatus = AgentRunStatus.RUNNING
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if self.status is not AgentRunStatus.RUNNING and self.ended_at is None:
            raise ValueError("ended_at is required for a terminal agent run")
        return self


class Company(VersionedRecord):
    company_id: UUID = Field(default_factory=uuid4)
    name: ShortStr
    founder_principal_id: UUID
    active_constitution_id: UUID
    status: CompanyStatus = CompanyStatus.ACTIVE


class OfferCell(VersionedRecord):
    cell_id: UUID = Field(default_factory=uuid4)
    company_id: UUID
    name: ShortStr
    objective: NonEmptyStr
    owner_principal_id: UUID
    budget_id: UUID
    state: CellStatus = CellStatus.DRAFT
    offer_version_ids: frozenset[UUID] = Field(default_factory=frozenset)
    amendment_history: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    ready_at: AwareDatetime | None = None
    activated_at: AwareDatetime | None = None
    closed_at: AwareDatetime | None = None

    @property
    def status(self) -> CellStatus:
        return self.state

    @model_validator(mode="after")
    def validate_cell_lifecycle(self) -> Self:
        after_ready = {
            CellStatus.READY,
            CellStatus.ACTIVE,
            CellStatus.PAUSED,
            CellStatus.SCALED,
            CellStatus.KILLED,
        }
        after_active = {
            CellStatus.ACTIVE,
            CellStatus.PAUSED,
            CellStatus.SCALED,
            CellStatus.KILLED,
        }
        if self.state in after_ready and self.ready_at is None:
            raise ValueError("ready_at is required after readiness")
        if self.state in after_active and self.activated_at is None:
            raise ValueError("activated_at is required after activation")
        if self.state in {CellStatus.SCALED, CellStatus.KILLED} and self.closed_at is None:
            raise ValueError("closed_at is required for a terminal cell")
        return self


class Budget(VersionedRecord):
    budget_id: UUID = Field(default_factory=uuid4)
    company_id: UUID
    scope: BudgetScope
    cell_id: UUID | None = None
    principal_id: UUID | None = None
    experiment_id: UUID | None = None
    limit_minor: int = Field(gt=0)
    spent_minor: int = Field(default=0, ge=0)
    reserved_minor: int = Field(default=0, ge=0)
    unit: UnitOfAccount = UnitOfAccount.MONEY
    currency: CurrencyCode | None = Field(default="USD", exclude_if=lambda value: value is None)
    status: BudgetStatus = BudgetStatus.ACTIVE
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @property
    def available_minor(self) -> int:
        return self.limit_minor - self.spent_minor - self.reserved_minor

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        if self.spent_minor + self.reserved_minor > self.limit_minor:
            raise ValueError("spent plus reserved cannot exceed budget limit")
        if self.unit is UnitOfAccount.MONEY:
            if self.currency is None:
                raise ValueError("money budgets require an ISO 4217 currency")
        elif self.currency is not None:
            raise ValueError("currency is only valid when unit is money")
        required = {
            BudgetScope.CELL: ("cell_id", self.cell_id),
            BudgetScope.AGENT: ("principal_id", self.principal_id),
            BudgetScope.EXPERIMENT: ("experiment_id", self.experiment_id),
        }
        if self.scope in required:
            field_name, value = required[self.scope]
            if value is None:
                raise ValueError(f"{field_name} is required for {self.scope.value} budget")
        return self


class BudgetReservation(VersionedRecord):
    reservation_id: UUID = Field(default_factory=uuid4)
    budget_id: UUID
    company_id: UUID
    amount_minor: int = Field(gt=0)
    unit: UnitOfAccount = UnitOfAccount.MONEY
    currency: CurrencyCode | None = Field(default=None, exclude_if=lambda value: value is None)
    purpose: NonEmptyStr
    idempotency_key: IdempotencyKey
    principal_id: UUID | None = None
    experiment_id: UUID | None = None
    work_item_id: UUID | None = None
    status: BudgetReservationStatus = BudgetReservationStatus.RESERVED
    reserved_at: AwareDatetime
    spent_at: AwareDatetime | None = None
    released_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_reservation(self) -> Self:
        if self.unit is UnitOfAccount.MONEY:
            if self.currency is None:
                raise ValueError("money reservations require an ISO 4217 currency")
        elif self.currency is not None:
            raise ValueError("currency is only valid when unit is money")
        if self.status is BudgetReservationStatus.SPENT and self.spent_at is None:
            raise ValueError("spent_at is required for spent reservation")
        if (
            self.status
            in {
                BudgetReservationStatus.RELEASED,
                BudgetReservationStatus.CANCELLED,
            }
            and self.released_at is None
        ):
            raise ValueError("released_at is required for released reservation")
        return self


class WorkItemState(FrozenContract):
    """Runtime claim/result projection around an exact WorkItem contract."""

    item: WorkItem
    attempt: int = Field(default=0, ge=0)
    lease_id: UUID | None = None
    result_evidence_ids: frozenset[UUID] = Field(default_factory=frozenset)
    actual_cost_minor: int = Field(default=0, ge=0)
    failure_code: Identifier | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.attempt > self.item.max_attempts:
            raise ValueError("attempt cannot exceed max_attempts")
        if self.item.status is WorkStatus.CLAIMED and self.lease_id is None:
            raise ValueError("claimed work requires lease_id")
        if self.item.status is not WorkStatus.CLAIMED and self.lease_id is not None:
            raise ValueError("non-claimed work cannot retain lease_id")
        if self.item.status is WorkStatus.COMPLETED and not self.result_evidence_ids:
            raise ValueError("completed work requires result evidence")
        if self.item.status is WorkStatus.FAILED and self.failure_code is None:
            raise ValueError("failed work requires failure_code")
        if self.actual_cost_minor > self.item.budget.maximum_cost_minor:
            raise ValueError("actual cost exceeds work budget")
        return self


class WorkLease(VersionedRecord):
    lease_id: UUID = Field(default_factory=uuid4)
    work_item_id: UUID
    company_id: UUID
    cell_id: UUID | None
    principal_id: UUID
    agent_run_id: UUID
    token: IdempotencyKey
    acquired_at: AwareDatetime
    expires_at: AwareDatetime
    status: LeaseStatus = LeaseStatus.ACTIVE
    released_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_lease(self) -> Self:
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must be later than acquired_at")
        if self.status is not LeaseStatus.ACTIVE and self.released_at is None:
            raise ValueError("released_at is required for an inactive lease")
        return self

    def is_live_at(self, at: datetime) -> bool:
        checked_at = _ensure_utc(at)
        return self.status is LeaseStatus.ACTIVE and checked_at < self.expires_at


class LearningCandidate(VersionedRecord):
    learning_id: UUID = Field(default_factory=uuid4)
    company_id: UUID
    cell_id: UUID
    source_experiment_id: UUID
    source_evaluation_id: UUID
    statement: NonEmptyStr
    confidence_bps: BasisPoints
    proposed_by_principal_id: UUID
    validated_by_principal_id: UUID | None = None
    evidence_ids: frozenset[UUID] = Field(min_length=1)
    status: LearningStatus = LearningStatus.CANDIDATE

    @model_validator(mode="after")
    def validate_independent_learning(self) -> Self:
        if self.validated_by_principal_id == self.proposed_by_principal_id:
            raise ValueError("learning validator must differ from proposer")
        if (
            self.status in {LearningStatus.VALIDATED, LearningStatus.PROMOTED}
            and self.validated_by_principal_id is None
        ):
            raise ValueError("validated_by_principal_id is required after validation")
        return self


_PROHIBITED_EVENT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "chain_of_thought",
        "credentials",
        "password",
        "private_key",
        "reasoning_trace",
        "secret",
        "token",
    }
)
_PROHIBITED_EVENT_KEYS_COMPACT = frozenset(key.replace("_", "") for key in _PROHIBITED_EVENT_KEYS)


def _validate_event_payload(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    def walk(node: JsonValue) -> None:
        if isinstance(node, dict):
            for raw_key, child in node.items():
                key = raw_key.casefold().replace("-", "_").replace(" ", "_")
                if (
                    key in _PROHIBITED_EVENT_KEYS
                    or key.replace("_", "") in _PROHIBITED_EVENT_KEYS_COMPACT
                ):
                    raise ValueError(f"prohibited key in event payload: {raw_key}")
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError(f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes")
    return value


class DomainEvent(FrozenContract):
    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    aggregate_type: AggregateType
    aggregate_id: UUID
    aggregate_revision: int = Field(ge=1)
    event_type: ShortStr
    actor_id: UUID
    company_id: UUID
    cell_id: UUID | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: IdempotencyKey | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_event_payload(value)


__all__ = [
    "TIME_SCALE",
    "AgentBinding",
    "AgentConstraints",
    "AgentDefinition",
    "AgentRole",
    "AgentRun",
    "AgentRunStatus",
    "AgentStatus",
    "AggregateType",
    "Allocation",
    "AllocationAction",
    "AllocationRanking",
    "AllocationStage",
    "AllocationStatus",
    "AllocationTarget",
    "AllocationTargetKind",
    "ApprovalRequirement",
    "AuthorityPolicy",
    "Budget",
    "BudgetAmount",
    "BudgetPolicy",
    "BudgetReservation",
    "BudgetReservationStatus",
    "BudgetScope",
    "BudgetStatus",
    "CausalDistance",
    "CellStatus",
    "CommercialModel",
    "Company",
    "CompanyStatus",
    "Constitution",
    "ConstitutionStatus",
    "CriterionResult",
    "DomainEvent",
    "Evaluation",
    "EvaluationVerdict",
    "EvidenceEnvelope",
    "EvidenceKind",
    "EvidenceLineage",
    "EvidenceObservation",
    "EvidenceRelation",
    "EvidenceSource",
    "EvidenceType",
    "EvidenceVerification",
    "Experiment",
    "ExperimentStatus",
    "ExperimentWindow",
    "FitnessPolicy",
    "GateDecision",
    "GateName",
    "GateResult",
    "IOContract",
    "IndependenceAttestation",
    "IndependenceBasis",
    "LearningCandidate",
    "LearningStatus",
    "LeaseStatus",
    "Metric",
    "MetricDirection",
    "Money",
    "Offer",
    "OfferCell",
    "OfferStatus",
    "OfferVersion",
    "OutcomeContract",
    "ProvenanceQuality",
    "RankingFormula",
    "RankingInputSnapshot",
    "ResourceReference",
    "RiskPolicy",
    "StopRule",
    "StopRuleEffect",
    "Threshold",
    "ThresholdOperator",
    "UnitOfAccount",
    "WorkBudget",
    "WorkItem",
    "WorkItemState",
    "WorkLease",
    "WorkStatus",
    "utc_now",
]

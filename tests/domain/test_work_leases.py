"""Contract tests for binding-scoped, expiring work leases."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_nature_company.domain.work import (
    cancel_work,
    claim_work,
    complete_work,
    fail_work,
    renew_lease,
)
from ai_nature_company.errors import LeaseError, ValidationError
from ai_nature_company.models import (
    AgentBinding,
    AgentConstraints,
    AgentDefinition,
    AgentRole,
    ApprovalRequirement,
    IOContract,
    OutcomeContract,
    WorkBudget,
    WorkItem,
    WorkItemState,
    WorkStatus,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_identity(*, capability: str = "work.item.execute") -> tuple[AgentDefinition, AgentBinding]:
    definition = AgentDefinition(
        definition_id=uuid4(),
        version=1,
        name="market-specialist",
        purpose="Complete bounded market work.",
        instructions=("Claim work before acting.",),
        declared_capabilities=frozenset({capability}),
        compatible_roles=frozenset({AgentRole.SPECIALIST}),
        constraints=AgentConstraints(
            may_commit_allocation=False,
            requires_active_lease_for_work=True,
        ),
        input_contract=IOContract(schema_ref="schemas/work-item-v1.json"),
        output_contract=IOContract(schema_ref="schemas/evidence-v1.json"),
    )
    binding = AgentBinding(
        company_id=uuid4(),
        principal_id=uuid4(),
        definition_id=definition.definition_id,
        definition_version=definition.version,
        cell_id=uuid4(),
        created_at=NOW,
        updated_at=NOW,
    )
    return definition, binding


def make_work(binding: AgentBinding, *, max_attempts: int = 3) -> WorkItemState:
    item = WorkItem(
        work_item_id=uuid4(),
        company_id=binding.company_id,
        cell_id=binding.cell_id,
        experiment_id=uuid4(),
        revision=1,
        status=WorkStatus.OPEN,
        kind="market_research",
        outcome_contract=OutcomeContract(
            outcome="Record one attributable market observation.",
            acceptance_criteria=("Evidence validates against evidence-v1.",),
            result_schema_ref="schemas/evidence-v1.json",
        ),
        eligible_capabilities=frozenset({"work.item.execute"}),
        budget=WorkBudget(currency="USD", maximum_cost_minor=500),
        approval_requirements=frozenset({ApprovalRequirement.NONE}),
        input_references=(),
        max_attempts=max_attempts,
        created_by_principal_id=uuid4(),
        created_at=NOW,
        available_at=NOW,
        deadline_at=NOW + timedelta(days=1),
    )
    return WorkItemState(item=item, updated_at=NOW)


def claim(
    state: WorkItemState,
    definition: AgentDefinition,
    binding: AgentBinding,
    *,
    run_id,
    token="opaque-lease-token",
):
    return claim_work(
        state,
        binding,
        definition,
        effective_capabilities=frozenset(definition.declared_capabilities),
        agent_run_id=run_id,
        expected_revision=state.item.revision,
        at=NOW,
        lease_for=timedelta(minutes=5),
        token=token,
    )


def test_claim_requires_matching_tenant_and_effective_capability() -> None:
    definition, binding = make_identity(capability="work.item.write")
    state = make_work(binding)

    with pytest.raises(ValidationError, match="required capabilities"):
        claim(state, definition, binding, run_id=uuid4())

    capable_definition, other_binding = make_identity()
    with pytest.raises(ValidationError, match="different company"):
        claim_work(
            state,
            other_binding,
            capable_definition,
            effective_capabilities=frozenset({"work.item.execute"}),
            agent_run_id=uuid4(),
            expected_revision=1,
            at=NOW,
            lease_for=timedelta(minutes=5),
        )


def test_claim_and_complete_close_both_work_and_lease() -> None:
    definition, binding = make_identity()
    state = make_work(binding)
    run_id = uuid4()
    claimed, lease = claim(state, definition, binding, run_id=run_id)
    completed, released_lease = complete_work(
        claimed,
        lease,
        principal_id=binding.principal_id,
        agent_run_id=run_id,
        lease_token="opaque-lease-token",
        evidence_ids=frozenset({uuid4()}),
        actual_cost_minor=250,
        expected_revision=2,
        expected_lease_revision=0,
        at=NOW + timedelta(minutes=2),
    )

    assert state.item.status is WorkStatus.OPEN
    assert claimed.item.status is WorkStatus.CLAIMED
    assert completed.item.status is WorkStatus.COMPLETED
    assert completed.lease_id is None
    assert released_lease.status.value == "released"


def test_expired_or_wrong_token_lease_cannot_complete() -> None:
    definition, binding = make_identity()
    state = make_work(binding)
    run_id = uuid4()
    claimed, lease = claim_work(
        state,
        binding,
        definition,
        effective_capabilities=frozenset({"work.item.execute"}),
        agent_run_id=run_id,
        expected_revision=1,
        at=NOW,
        lease_for=timedelta(seconds=10),
        token="correct-token",
    )

    with pytest.raises(LeaseError, match="token"):
        complete_work(
            claimed,
            lease,
            principal_id=binding.principal_id,
            agent_run_id=run_id,
            lease_token="wrong-token",
            evidence_ids=frozenset({uuid4()}),
            actual_cost_minor=0,
            expected_revision=2,
            expected_lease_revision=0,
            at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(LeaseError, match="expired"):
        complete_work(
            claimed,
            lease,
            principal_id=binding.principal_id,
            agent_run_id=run_id,
            lease_token="correct-token",
            evidence_ids=frozenset({uuid4()}),
            actual_cost_minor=0,
            expected_revision=2,
            expected_lease_revision=0,
            at=NOW + timedelta(seconds=11),
        )


def test_lease_renewal_is_versioned_and_bound_to_run() -> None:
    definition, binding = make_identity()
    run_id = uuid4()
    _, lease = claim(make_work(binding), definition, binding, run_id=run_id, token="token")
    renewed = renew_lease(
        lease,
        principal_id=binding.principal_id,
        agent_run_id=run_id,
        lease_token="token",
        expected_revision=0,
        at=NOW + timedelta(minutes=1),
        lease_for=timedelta(minutes=10),
    )

    assert renewed.revision == 1
    assert renewed.expires_at == NOW + timedelta(minutes=11)


def test_retryable_failure_closes_final_attempt() -> None:
    definition, binding = make_identity()
    state = make_work(binding, max_attempts=1)
    run_id = uuid4()
    claimed, lease = claim(state, definition, binding, run_id=run_id, token="token")
    failed, released_lease = fail_work(
        claimed,
        lease,
        principal_id=binding.principal_id,
        agent_run_id=run_id,
        lease_token="token",
        failure_code="connector_timeout",
        retryable=True,
        expected_revision=2,
        expected_lease_revision=0,
        at=NOW + timedelta(minutes=1),
    )

    assert failed.item.status is WorkStatus.FAILED
    assert failed.failure_code == "connector_timeout"
    assert released_lease.status.value == "released"


def test_cancel_moves_never_claimed_open_work_to_cancelled() -> None:
    _definition, binding = make_identity()
    state = make_work(binding)
    cancelled, lease = cancel_work(
        state,
        expected_revision=state.item.revision,
        at=NOW + timedelta(minutes=1),
    )

    assert lease is None
    assert cancelled.item.status is WorkStatus.CANCELLED
    assert cancelled.lease_id is None


def test_cancel_refuses_claimed_work_without_its_lease() -> None:
    definition, binding = make_identity()
    claimed, _lease = claim(make_work(binding), definition, binding, run_id=uuid4())
    with pytest.raises(LeaseError, match="requires its lease"):
        cancel_work(
            claimed,
            expected_revision=claimed.item.revision,
            at=NOW + timedelta(minutes=1),
        )

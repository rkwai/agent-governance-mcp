"""Adversarial executable specification for all Phase 1 acceptance criteria."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

import pytest

from ai_nature_company.auth import (
    AgentIdentityContext,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.errors import (
    AuthorizationError,
    BudgetError,
    ConflictError,
    LeaseError,
    NotFoundError,
    SeparationOfDutiesError,
    ValidationError,
)

from .scenario import REQUIRED_GATES, Scenario
from .support import (
    ALLOCATION_ID,
    CELL_ID,
    COMPANY_ID,
    CONSTITUTION_ID,
    EVALUATION_ID,
    EVIDENCE_ID,
    EXPERIMENT_ID,
    MUTATED_OFFER_VERSION_ID,
    OFFER_VERSION_ID,
    OTHER_COMPANY_ID,
    PRINCIPAL_IDS,
    RUN_IDS,
    WORK_ITEM_ID,
    assert_mutation_response,
    assert_no_hidden_reasoning,
    load_definition,
    make_principal,
    mutation,
    stable_uuid,
)


def _state_record(response: dict[str, Any], key: str) -> dict[str, Any]:
    record = response["state"].get(key)
    assert isinstance(record, dict), f"mutation state must contain {key!r}"
    return record


def _money_number(value: object) -> Decimal:
    if isinstance(value, dict):
        value = value.get(
            "amount_minor",
            value.get("maximum_cost_minor", value.get("amount")),
        )
    return Decimal(str(value))


def _assert_mirrored_reservation_group(
    group: dict[str, Any],
    *,
    expected_minor: int,
    expected_status: str = "active",
) -> list[dict[str, Any]]:
    roles = {"company", "cell", "stage"}
    assert group["reservation_group_id"]
    assert group["status"] == expected_status
    assert _money_number(group["amount"]) * 100 == expected_minor
    assert set(group["reservation_ids"]) == roles
    assert set(group["budget_ids"]) == roles
    assert len(set(group["reservation_ids"].values())) == 3
    assert len(set(group["budget_ids"].values())) == 3

    members = group["members"]
    assert {member["ledger_role"] for member in members} == roles
    assert all(
        member["reservation_group_id"] == group["reservation_group_id"]
        and member["status"] == expected_status
        and _money_number(member["amount"]) * 100 == expected_minor
        and group["reservation_ids"][member["ledger_role"]] == member["reservation_id"]
        and group["budget_ids"][member["ledger_role"]] == member["budget_id"]
        for member in members
    )
    return members


@pytest.mark.asyncio
async def test_ac01_founder_bootstraps_company_and_active_constitution(
    bootstrapped: Scenario,
) -> None:
    response = bootstrapped.responses["bootstrap"]
    state = assert_mutation_response(response, prior_revision=0, new_revision=1)

    assert state["company"]["company_id"] == COMPANY_ID
    assert state["company"]["founder_principal_id"] == PRINCIPAL_IDS["founder"]
    assert state["company"]["active_constitution_id"] == CONSTITUTION_ID
    assert state["constitution"]["constitution_id"] == CONSTITUTION_ID
    assert state["constitution"]["version"] == 1
    assert state["constitution"]["status"] == "active"
    assert state["budget"]["budget_id"]

    described = await bootstrapped.service.invoke(
        "company.describe", bootstrapped.principals["founder"], {}
    )
    assert described["company"] == state["company"]
    assert described["constitution"] == state["constitution"]


@pytest.mark.asyncio
async def test_ac01_budget_ledgers_must_match_the_constitution_and_roll_back() -> None:
    bootstrap_cases = (
        {
            "budget_currency": "EUR",
            "key": "reject-bootstrap-budget-currency-mismatch",
        },
        {
            "budget_limit": "999.99",
            "key": "reject-bootstrap-budget-limit-mismatch",
        },
    )
    for bootstrap_kwargs in bootstrap_cases:
        rejected = Scenario()
        with pytest.raises(ValidationError):
            await rejected.bootstrap(**bootstrap_kwargs)
        exported = await rejected.store.export_company(COMPANY_ID)
        assert exported["projections"] == []
        assert exported["domain_events"] == []
        assert exported["audit_events"] == []
        assert exported["budgets"] == []
        assert exported["reservations"] == []
        assert exported["idempotency_records"] == []

    cell_case = Scenario()
    await cell_case.bootstrap()
    before = await cell_case.store.export_company(COMPANY_ID)
    with pytest.raises(ValidationError):
        await cell_case.create_cell(
            currency="EUR",
            key="reject-cell-budget-currency-mismatch",
        )
    with pytest.raises(BudgetError):
        await cell_case.create_cell(
            budget_limit="500.01",
            key="reject-cell-budget-limit-mismatch",
        )
    after = await cell_case.store.export_company(COMPANY_ID)
    assert after["projections"] == before["projections"]
    assert after["domain_events"] == before["domain_events"]
    assert after["audit_events"] == before["audit_events"]
    assert after["budgets"] == before["budgets"]
    assert after["reservations"] == before["reservations"]
    assert not any(projection["projection_type"] == "cell" for projection in after["projections"])


@pytest.mark.asyncio
async def test_cell_scaling_fails_closed_until_gate_decision_is_atomically_bound(
    offer_cell: Scenario,
) -> None:
    before = await offer_cell.store.export_company(COMPANY_ID)

    with pytest.raises(ValidationError, match="atomically bound"):
        await offer_cell.service.invoke(
            "cell.transition",
            offer_cell.principals["founder"],
            mutation(
                "cell.transition",
                {"cell_id": CELL_ID, "target_status": "scaled"},
                expected_revision=offer_cell.cell_revision,
                key="reject-ungated-cell-scale",
            ),
        )

    after = await offer_cell.store.export_company(COMPANY_ID)
    assert {key: value for key, value in after.items() if key != "exported_at"} == {
        key: value for key, value in before.items() if key != "exported_at"
    }


@pytest.mark.asyncio
async def test_paused_cell_reactivation_fails_closed_until_remediation_is_bound(
    offer_cell: Scenario,
) -> None:
    await offer_cell.invoke(
        "cell.transition",
        "immune",
        {"cell_id": CELL_ID, "target_status": "paused"},
        expected_revision=offer_cell.cell_revision,
        key="pause-before-reactivation-check",
    )
    before = await offer_cell.store.export_company(COMPANY_ID)

    with pytest.raises(ValidationError, match="remediation, approval, and budget"):
        await offer_cell.service.invoke(
            "cell.transition",
            offer_cell.principals["owner"],
            mutation(
                "cell.transition",
                {"cell_id": CELL_ID, "target_status": "active"},
                expected_revision=offer_cell.cell_revision,
                key="reject-unbound-cell-reactivation",
            ),
        )

    after = await offer_cell.store.export_company(COMPANY_ID)
    assert {key: value for key, value in after.items() if key != "exported_at"} == {
        key: value for key, value in before.items() if key != "exported_at"
    }


@pytest.mark.asyncio
async def test_ac02_versioned_role_definitions_are_registered(
    bootstrapped: Scenario,
) -> None:
    agents = bootstrapped.responses["bootstrap"]["state"]["agents"]
    by_role = {
        agent["compatible_roles"][0]: agent
        for agent in agents
        if len(agent["compatible_roles"]) == 1
    }

    assert {"cell_owner", "specialist", "evaluator", "allocator"} <= set(by_role)
    for kind, role in {
        "cell-owner": "cell_owner",
        "specialist": "specialist",
        "evaluator": "evaluator",
        "allocator": "allocator",
    }.items():
        manifest = load_definition(kind)
        registered = by_role[role]
        assert registered["version"] == manifest["version"]
        assert set(registered["declared_capabilities"]) == set(manifest["declared_capabilities"])

    listed = await bootstrapped.service.invoke("agent.list", bootstrapped.principals["founder"], {})
    assert {agent["definition_id"] for agent in listed["agents"]} == {
        agent["definition_id"] for agent in agents
    }


@pytest.mark.asyncio
async def test_agent_definition_can_back_multiple_concrete_instances(
    bootstrapped: Scenario,
) -> None:
    definition = load_definition("specialist")
    second_instance_id = stable_uuid("second-specialist-instance")
    second_principal_id = stable_uuid("second-specialist-principal")
    second_cell_id = stable_uuid("second-specialist-cell")

    response = await bootstrapped.service.invoke(
        "agent.register",
        bootstrapped.principals["founder"],
        mutation(
            "agent.register",
            {
                "agent_instance_id": second_instance_id,
                "principal_id": second_principal_id,
                "role": "specialist",
                "cell_id": second_cell_id,
                "definition": definition,
            },
            expected_revision=0,
            key="register-second-specialist-instance",
        ),
    )
    state = assert_mutation_response(dict(response), prior_revision=0)

    assert state["agent"]["agent_instance_id"] == second_instance_id
    assert state["agent"]["agent_id"] == second_instance_id
    assert state["agent"]["definition_id"] == definition["definition_id"]
    assert state["agent"]["principal_id"] == second_principal_id
    assert state["agent"]["cell_id"] == second_cell_id
    assert state["binding"]["agent_instance_id"] == second_instance_id

    fetched = await bootstrapped.service.invoke(
        "agent.get",
        bootstrapped.principals["founder"],
        {"agent_instance_id": second_instance_id},
    )
    assert fetched["agent"] == state["agent"]

    exported = await bootstrapped.store.export_company(COMPANY_ID)
    specialist_definitions = [
        projection
        for projection in exported["projections"]
        if projection["projection_type"] == "agent_definition"
        and projection["projection_id"] == definition["definition_id"]
    ]
    specialist_instances = [
        projection
        for projection in exported["projections"]
        if projection["projection_type"] == "agent"
        and projection["state"].get("definition_id") == definition["definition_id"]
    ]
    assert len(specialist_definitions) == 1
    assert len(specialist_instances) == 2


@pytest.mark.asyncio
async def test_agent_instance_role_must_be_compatible_with_definition(
    bootstrapped: Scenario,
) -> None:
    with pytest.raises(ValidationError, match="role is not compatible"):
        await bootstrapped.service.invoke(
            "agent.register",
            bootstrapped.principals["founder"],
            mutation(
                "agent.register",
                {
                    "agent_instance_id": stable_uuid("misbound-agent-instance"),
                    "principal_id": stable_uuid("misbound-agent-principal"),
                    "role": "evaluator",
                    "cell_id": CELL_ID,
                    "definition": load_definition("specialist"),
                },
                expected_revision=0,
                key="reject-incompatible-agent-instance-role",
            ),
        )


@pytest.mark.asyncio
async def test_trusted_agent_identity_is_described_and_filters_cell_discovery(
    bootstrapped: Scenario,
) -> None:
    await bootstrapped.create_cell()
    other_cell_id = stable_uuid("identity-filter-other-cell")
    await bootstrapped.invoke(
        "cell.create",
        "founder",
        {
            "cell_id": other_cell_id,
            "name": "Unrelated cell",
            "objective": "Remain outside the bound owner's discovery scope.",
            "budget_id": stable_uuid("identity-filter-other-cell-budget"),
            "budget_limit": "100.00",
            "currency": "USD",
            "budget_duration_days": 90,
        },
        expected_revision=0,
        key="create-identity-filter-other-cell",
    )

    definition = load_definition("cell-owner")
    agent_identity = AgentIdentityContext(
        agent_instance_id=definition["definition_id"],
        definition_id=definition["definition_id"],
        definition_version=definition["version"],
        role=Role.CELL_OWNER,
        cell_id=CELL_ID,
    )
    record = PrincipalRecord(
        principal_id=PRINCIPAL_IDS["owner"],
        grants=(ScopedRoleGrant(Role.CELL_OWNER, ResourceScope.company(COMPANY_ID)),),
        agent_run_id=RUN_IDS["owner"],
        agent_identity=agent_identity,
    )
    owner = LocalTestAuthenticator({"identity-aware-owner": record}).authenticate(
        "identity-aware-owner"
    )

    described = await bootstrapped.service.invoke("company.describe", owner, {})
    assert described["principal"] == {
        "principal_id": PRINCIPAL_IDS["owner"],
        "roles": ["cell_owner"],
        "effective_capabilities": sorted(definition["declared_capabilities"]),
        "agent_instance_id": definition["definition_id"],
        "definition_id": definition["definition_id"],
        "definition_version": definition["version"],
        "role": "cell_owner",
        "cell_id": CELL_ID,
        "agent_run_id": RUN_IDS["owner"],
    }

    listed = await bootstrapped.service.invoke("cell.list", owner, {})
    assert [cell["cell_id"] for cell in listed["cells"]] == [CELL_ID]
    with pytest.raises(AuthorizationError, match="does not cover"):
        await bootstrapped.service.invoke("cell.get", owner, {"cell_id": other_cell_id})


@pytest.mark.asyncio
async def test_describe_tools_surface_caller_run_expiry(bootstrapped: Scenario) -> None:
    expires_at = bootstrapped.clock.current + timedelta(hours=24)
    definition = load_definition("cell-owner")
    owner = LocalTestAuthenticator(
        {
            "expiring-owner": PrincipalRecord(
                principal_id=PRINCIPAL_IDS["owner"],
                grants=(ScopedRoleGrant(Role.CELL_OWNER, ResourceScope.company(COMPANY_ID)),),
                agent_run_id=RUN_IDS["owner"],
                agent_identity=AgentIdentityContext(
                    agent_instance_id=definition["definition_id"],
                    definition_id=definition["definition_id"],
                    definition_version=definition["version"],
                    role=Role.CELL_OWNER,
                    cell_id=CELL_ID,
                ),
                agent_run_expires_at=expires_at,
            )
        }
    ).authenticate("expiring-owner")

    described = await bootstrapped.service.invoke("company.describe", owner, {})
    protocol = await bootstrapped.service.invoke("protocol.describe", owner, {})

    assert described["principal"]["agent_run_expires_at"] == "2026-08-30T12:00:00Z"
    assert described["principal"]["agent_run_expires_soon"] is True
    assert protocol["caller_agent_run"] == {
        "agent_run_id": RUN_IDS["owner"],
        "expires_at": "2026-08-30T12:00:00Z",
        "expires_soon": True,
    }


@pytest.mark.asyncio
async def test_agent_definition_ceiling_applies_to_read_tools(
    bootstrapped: Scenario,
) -> None:
    definition = load_definition("specialist")
    agent_identity = AgentIdentityContext(
        agent_instance_id=definition["definition_id"],
        definition_id=definition["definition_id"],
        definition_version=definition["version"],
        role=Role.SPECIALIST,
        cell_id=CELL_ID,
    )
    record = PrincipalRecord(
        principal_id=PRINCIPAL_IDS["specialist"],
        grants=(ScopedRoleGrant(Role.SPECIALIST, ResourceScope.company(COMPANY_ID)),),
        agent_run_id=RUN_IDS["specialist"],
        agent_identity=agent_identity,
    )
    specialist = LocalTestAuthenticator({"specialist": record}).authenticate("specialist")

    described = await bootstrapped.service.invoke("company.describe", specialist, {})
    assert "company.describe" in described["principal"]["effective_capabilities"]
    assert "cell.list" not in definition["declared_capabilities"]
    with pytest.raises(AuthorizationError, match="definition does not permit"):
        await bootstrapped.service.invoke("cell.list", specialist, {})


@pytest.mark.asyncio
async def test_trusted_agent_identity_requires_its_immutable_definition_projection(
    bootstrapped: Scenario,
) -> None:
    definition_id = stable_uuid("missing-agent-definition")
    instance_id = stable_uuid("missing-definition-agent-instance")
    principal_id = stable_uuid("missing-definition-agent-principal")
    run_id = stable_uuid("missing-definition-agent-run")
    identity = AgentIdentityContext(
        agent_instance_id=instance_id,
        definition_id=definition_id,
        definition_version=1,
        role=Role.CELL_OWNER,
        cell_id=CELL_ID,
    )
    async with bootstrapped.store.transaction(COMPANY_ID) as tx:
        await tx.put_projection(
            projection_type="agent",
            projection_id=instance_id,
            state={
                "agent_instance_id": instance_id,
                "principal_id": principal_id,
                "definition_id": definition_id,
                "definition_version": 1,
                "role": "cell_owner",
                "cell_id": CELL_ID,
                "status": "active",
                "capabilities": ["company.describe"],
                "allowed_actions": ["company.describe"],
            },
            expected_revision=0,
        )
        await tx.put_projection(
            projection_type="agent_binding",
            projection_id=principal_id,
            state={
                "principal_id": principal_id,
                "agent_instance_id": instance_id,
                "definition_id": definition_id,
                "definition_version": 1,
                "role": "cell_owner",
                "cell_id": CELL_ID,
                "status": "active",
            },
            expected_revision=0,
        )
    principal = LocalTestAuthenticator(
        {
            "missing-definition": PrincipalRecord(
                principal_id=principal_id,
                grants=(ScopedRoleGrant(Role.CELL_OWNER, ResourceScope.company(COMPANY_ID)),),
                agent_run_id=run_id,
                agent_identity=identity,
            )
        }
    ).authenticate("missing-definition")

    with pytest.raises(AuthorizationError, match="definition does not exist"):
        await bootstrapped.service.invoke("company.describe", principal, {})


@pytest.mark.asyncio
async def test_ac03_discovery_is_principal_scoped_and_least_privilege(
    bootstrapped: Scenario,
) -> None:
    definitions = {
        "owner": load_definition("cell-owner"),
        "specialist": load_definition("specialist"),
        "evaluator": load_definition("evaluator"),
        "allocator": load_definition("allocator"),
        "immune": load_definition("immune"),
    }
    descriptions: dict[str, dict[str, Any]] = {}
    for actor, definition in definitions.items():
        described = await bootstrapped.service.invoke(
            "company.describe",
            bootstrapped.principals[actor],
            {
                "principal_id": PRINCIPAL_IDS["founder"],
                "role": "founder",
                "company_id": OTHER_COMPANY_ID,
            },
        )
        descriptions[actor] = dict(described)
        principal = described["principal"]
        assert principal["principal_id"] == PRINCIPAL_IDS[actor]
        assert principal["roles"] == [definition["compatible_roles"][0]]
        assert set(principal["effective_capabilities"]) <= set(definition["declared_capabilities"])

    assert "offer.mutate" in descriptions["owner"]["principal"]["effective_capabilities"]
    assert "allocation.commit" in descriptions["owner"]["principal"]["effective_capabilities"]
    assert "evaluation.submit" not in descriptions["owner"]["principal"]["effective_capabilities"]
    assert "evaluation.submit" in descriptions["evaluator"]["principal"]["effective_capabilities"]
    assert (
        "experiment.start" not in descriptions["evaluator"]["principal"]["effective_capabilities"]
    )
    assert "experiment.amend" in descriptions["owner"]["principal"]["effective_capabilities"]
    assert (
        "experiment.amend" not in descriptions["evaluator"]["principal"]["effective_capabilities"]
    )
    assert "cell.amend" in descriptions["owner"]["principal"]["effective_capabilities"]
    assert "cell.amend" not in descriptions["evaluator"]["principal"]["effective_capabilities"]
    assert "experiment.record_send" in descriptions["owner"]["principal"]["effective_capabilities"]
    assert (
        "experiment.record_send"
        not in descriptions["evaluator"]["principal"]["effective_capabilities"]
    )
    assert "work.cancel" in descriptions["owner"]["principal"]["effective_capabilities"]
    assert "work.cancel" not in descriptions["evaluator"]["principal"]["effective_capabilities"]


@pytest.mark.asyncio
async def test_ac04_offer_mutation_creates_an_immutable_child(
    bootstrapped: Scenario,
) -> None:
    await bootstrapped.create_cell()
    registered_response = await bootstrapped.register_offer()
    parent_before = deepcopy(_state_record(registered_response, "offer"))
    mutated_response = await bootstrapped.mutate_offer()
    child = _state_record(mutated_response, "offer")

    assert child["offer_version_id"] == MUTATED_OFFER_VERSION_ID
    assert child["offer_id"] == parent_before["offer_id"]
    assert child["version"] == 2
    assert child["parent_offer_version_id"] == OFFER_VERSION_ID
    assert child["mutation_summary"]
    assert child["target_segment"] != parent_before["target_segment"]

    exported = await bootstrapped.store.export_company(COMPANY_ID)
    offer_versions = [
        item["state"]
        for item in exported["projections"]
        if item["projection_type"] in {"offer", "offer_version"}
    ]
    assert parent_before in offer_versions
    assert child in offer_versions


@pytest.mark.asyncio
async def test_ac05_bounded_work_has_one_eligible_expiring_lease(
    running_experiment: Scenario,
) -> None:
    created = await running_experiment.create_work()
    work = _state_record(created, "work_item")
    assert work["outcome_contract"]["acceptance_criteria"]
    assert work["eligible_capabilities"] == ["evidence.append"]
    assert _money_number(work["budget"]) == 1_000

    claimed = await running_experiment.claim_work()
    claimed_work = _state_record(claimed, "work_item")
    lease = _state_record(claimed, "lease")
    assert claimed_work["status"] == "claimed"
    assert lease["principal_id"] == PRINCIPAL_IDS["specialist"]
    assert lease["agent_run_id"] == RUN_IDS["specialist"]
    assert lease["expires_at"] > lease["acquired_at"]
    assert lease["token"] not in json.dumps(claimed_work)
    predictable_token = uuid5(UUID(claimed["operation_id"]), "work-lease-token").hex
    assert lease["token"] != predictable_token

    before_oversized_heartbeat = await running_experiment.store.export_company(COMPANY_ID)
    with pytest.raises(LeaseError):
        await running_experiment.service.invoke(
            "work.heartbeat",
            running_experiment.principals["specialist"],
            mutation(
                "work.heartbeat",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "lease_token": lease["token"],
                    "lease_seconds": 86_401,
                },
                expected_revision=running_experiment.work_revision,
                key="reject-oversized-heartbeat",
            ),
        )
    after_oversized_heartbeat = await running_experiment.store.export_company(COMPANY_ID)
    assert len(after_oversized_heartbeat["domain_events"]) == len(
        before_oversized_heartbeat["domain_events"]
    )

    exported = await running_experiment.store.export_company(COMPANY_ID)
    live = [item for item in exported["work_leases"] if item["status"] == "active"]
    assert len(live) == 1

    with pytest.raises((AuthorizationError, ConflictError)):
        await running_experiment.claim_work(
            expected_revision=work["revision"],
            key="ineligible-racing-claim",
            actor="evaluator",
        )


@pytest.mark.asyncio
async def test_ac05_heartbeat_requires_the_claiming_agents_manifest_capability(
    running_experiment: Scenario,
) -> None:
    limited_specialist_id = stable_uuid("limited-specialist-principal")
    limited_definition = deepcopy(load_definition("specialist"))
    limited_definition.update(
        definition_id=stable_uuid("limited-specialist-definition"),
        name="limited-specialist",
        declared_capabilities=[
            capability
            for capability in limited_definition["declared_capabilities"]
            if capability != "work.heartbeat"
        ],
    )
    await running_experiment.invoke(
        "agent.register",
        "founder",
        {
            "principal_id": limited_specialist_id,
            "cell_id": CELL_ID,
            "definition": limited_definition,
        },
        expected_revision=0,
        key="register-limited-specialist",
    )
    running_experiment.principals["limited_specialist"] = make_principal(
        Role.SPECIALIST,
        principal_id=limited_specialist_id,
        agent_run_id=stable_uuid("limited-specialist-run"),
    )
    await running_experiment.create_work()
    claim = await running_experiment.claim_work(actor="limited_specialist")
    lease = _state_record(claim, "lease")
    before = await running_experiment.store.export_company(COMPANY_ID)

    with pytest.raises(AuthorizationError):
        await running_experiment.service.invoke(
            "work.heartbeat",
            running_experiment.principals["limited_specialist"],
            mutation(
                "work.heartbeat",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "lease_token": lease["token"],
                    "lease_seconds": 300,
                },
                expected_revision=running_experiment.work_revision,
                key="manifest-does-not-permit-heartbeat",
            ),
        )
    after = await running_experiment.store.export_company(COMPANY_ID)
    assert len(after["domain_events"]) == len(before["domain_events"])


@pytest.mark.asyncio
async def test_ac05_work_derives_its_cell_from_the_running_experiment(
    running_experiment: Scenario,
) -> None:
    response = await running_experiment.create_work(
        cell_id=None,
        key="derive-work-cell-from-experiment",
    )
    work = _state_record(response, "work_item")

    assert work["cell_id"] == CELL_ID
    assert work["experiment_id"] == EXPERIMENT_ID


@pytest.mark.asyncio
async def test_ac05_cell_bound_agent_cannot_create_unscoped_work(
    running_experiment: Scenario,
) -> None:
    definition = load_definition("cell-owner")
    owner = LocalTestAuthenticator(
        {
            "identity-aware-owner": PrincipalRecord(
                principal_id=PRINCIPAL_IDS["owner"],
                grants=(
                    ScopedRoleGrant(
                        Role.CELL_OWNER,
                        ResourceScope.company(COMPANY_ID),
                    ),
                ),
                agent_run_id=RUN_IDS["owner"],
                agent_identity=AgentIdentityContext(
                    agent_instance_id=definition["definition_id"],
                    definition_id=definition["definition_id"],
                    definition_version=definition["version"],
                    role=Role.CELL_OWNER,
                    cell_id=CELL_ID,
                ),
            )
        }
    ).authenticate("identity-aware-owner")
    running_experiment.principals["owner"] = owner
    before = await running_experiment.store.export_company(COMPANY_ID)

    with pytest.raises(AuthorizationError, match="does not cover this work cell"):
        await running_experiment.create_work(
            cell_id=None,
            experiment_id=None,
            key="reject-cell-agent-unscoped-work",
        )

    after = await running_experiment.store.export_company(COMPANY_ID)
    assert after["domain_events"] == before["domain_events"]
    assert after["audit_events"] == before["audit_events"]
    assert not any(
        projection["projection_type"] == "work_item" and projection["projection_id"] == WORK_ITEM_ID
        for projection in after["projections"]
    )


@pytest.mark.asyncio
async def test_ac05_founder_can_create_company_scoped_work_without_a_cell(
    bootstrapped: Scenario,
) -> None:
    work_item_id = stable_uuid("founder-company-scoped-work")

    response = await bootstrapped.invoke(
        "work.create",
        "founder",
        {
            "work_item_id": work_item_id,
            "cell_id": None,
            "experiment_id": None,
            "kind": "company_scoped_governance_work",
            "outcome_contract": {
                "outcome": "Record one company-scoped governance result.",
                "acceptance_criteria": ["The result is attributable to the founder."],
                "result_schema_ref": "schemas/evidence-v1.json",
            },
            "eligible_capabilities": ["evidence.append"],
            "budget": {"currency": "USD", "maximum_cost_minor": 0},
            "approval_requirements": ["none"],
            "input_references": [],
            "max_attempts": 1,
        },
        expected_revision=0,
        key="founder-company-scoped-work",
    )
    work = _state_record(response, "work_item")

    assert work["cell_id"] is None
    assert work["experiment_id"] is None

    before_claim = await bootstrapped.store.export_company(COMPANY_ID)
    with pytest.raises(AuthorizationError, match="company-scoped target"):
        await bootstrapped.service.invoke(
            "work.claim",
            bootstrapped.principals["specialist"],
            mutation(
                "work.claim",
                {"work_item_id": work_item_id, "lease_seconds": 300},
                expected_revision=1,
                key="reject-cell-agent-company-work-claim",
            ),
        )
    after_claim = await bootstrapped.store.export_company(COMPANY_ID)
    assert after_claim["domain_events"] == before_claim["domain_events"]
    assert after_claim["work_leases"] == before_claim["work_leases"]


@pytest.mark.asyncio
async def test_ac05_cell_agent_cannot_reconcile_expired_company_scoped_work(
    bootstrapped: Scenario,
) -> None:
    work_item_id = stable_uuid("expired-founder-company-scoped-work")
    definition = load_definition("specialist")
    instance_id = stable_uuid("company-scoped-specialist-instance")
    principal_id = stable_uuid("company-scoped-specialist-principal")
    run_id = stable_uuid("company-scoped-specialist-run")
    await bootstrapped.invoke(
        "agent.register",
        "founder",
        {
            "agent_instance_id": instance_id,
            "principal_id": principal_id,
            "role": "specialist",
            "cell_id": None,
            "definition": definition,
        },
        expected_revision=0,
        key="register-company-scoped-specialist",
    )
    bootstrapped.principals["company_specialist"] = LocalTestAuthenticator(
        {
            "company-specialist": PrincipalRecord(
                principal_id=principal_id,
                grants=(
                    ScopedRoleGrant(
                        Role.SPECIALIST,
                        ResourceScope.company(COMPANY_ID),
                    ),
                ),
                agent_run_id=run_id,
                agent_identity=AgentIdentityContext(
                    agent_instance_id=instance_id,
                    definition_id=definition["definition_id"],
                    definition_version=definition["version"],
                    role=Role.SPECIALIST,
                    cell_id=None,
                ),
            )
        }
    ).authenticate("company-specialist")
    await bootstrapped.invoke(
        "work.create",
        "founder",
        {
            "work_item_id": work_item_id,
            "cell_id": None,
            "experiment_id": None,
            "kind": "company_scoped_governance_work",
            "outcome_contract": {
                "outcome": "Record one company-scoped governance result.",
                "acceptance_criteria": ["The result is attributable to the founder."],
                "result_schema_ref": "schemas/evidence-v1.json",
            },
            "eligible_capabilities": ["evidence.append"],
            "budget": {"currency": "USD", "maximum_cost_minor": 0},
            "approval_requirements": ["none"],
            "input_references": [],
            "max_attempts": 1,
        },
        expected_revision=0,
        key="create-expiring-founder-company-work",
    )
    await bootstrapped.invoke(
        "work.claim",
        "company_specialist",
        {"work_item_id": work_item_id, "lease_seconds": 1},
        expected_revision=1,
        key="founder-claims-company-work",
    )
    bootstrapped.clock.advance(seconds=2)
    before = await bootstrapped.store.export_company(COMPANY_ID)

    with pytest.raises(AuthorizationError, match="company-scoped target"):
        await bootstrapped.service.invoke(
            "work.claim",
            bootstrapped.principals["specialist"],
            mutation(
                "work.claim",
                {"work_item_id": work_item_id, "lease_seconds": 300},
                expected_revision=2,
                key="reject-cell-agent-expiry-reconciliation",
            ),
        )

    after = await bootstrapped.store.export_company(COMPANY_ID)
    assert after["domain_events"] == before["domain_events"]
    assert after["audit_events"] == before["audit_events"]
    assert after["projections"] == before["projections"]
    assert after["work_leases"] == before["work_leases"]


@pytest.mark.asyncio
async def test_ac05_expiry_at_attempt_limit_fails_without_an_extra_lease(
    running_experiment: Scenario,
) -> None:
    await running_experiment.create_work(max_attempts=1)
    await running_experiment.claim_work(lease_seconds=1)
    running_experiment.clock.advance(seconds=2)

    reconciled = await running_experiment.claim_work(
        expected_revision=running_experiment.work_revision,
        key="reconcile-expired-final-attempt",
    )
    work = _state_record(reconciled, "work_item")
    assert work["status"] == "failed"
    assert reconciled["state"]["lease"] is None
    assert reconciled["state"]["reconciled_expiry"] is True

    exported = await running_experiment.store.export_company(COMPANY_ID)
    runtime = next(
        projection["state"]
        for projection in exported["projections"]
        if projection["projection_type"] == "work_runtime"
        and projection["projection_id"] == WORK_ITEM_ID
    )
    assert runtime["attempt"] == 1
    assert runtime["failure_code"] == "lease_expired"
    assert not any(item["status"] == "active" for item in exported["work_leases"])


@pytest.mark.asyncio
async def test_ac06_experiment_preregistration_freezes_budget_window_and_rules(
    offer_cell: Scenario,
) -> None:
    registered = await offer_cell.register_experiment()
    experiment = _state_record(registered, "experiment")

    assert experiment["state"] == "registered"
    assert experiment["hypothesis"]
    assert experiment["success_thresholds"]
    assert experiment["failure_thresholds"]
    assert experiment["stop_rules"]
    assert experiment["window"]["ends_at"] > experiment["window"]["starts_at"]
    assert _money_number(experiment["budget"]) == 10_000
    assert experiment["executor_principal_ids"] == [PRINCIPAL_IDS["specialist"]]
    assert experiment["evaluator_principal_id"] == PRINCIPAL_IDS["evaluator"]

    before_rewrite = await offer_cell.store.export_company(COMPANY_ID)
    with pytest.raises(ConflictError):
        await offer_cell.register_experiment(
            budget_minor=9_000,
            key="attempt-material-preregistration-rewrite",
        )
    after_rewrite = await offer_cell.store.export_company(COMPANY_ID)
    assert len(after_rewrite["domain_events"]) == len(before_rewrite["domain_events"])
    persisted = next(
        projection["state"]
        for projection in after_rewrite["projections"]
        if projection["projection_type"] == "experiment"
        and projection["projection_id"] == EXPERIMENT_ID
    )
    assert persisted == experiment


@pytest.mark.asyncio
async def test_ac07_experiment_start_atomically_reserves_its_fixed_budget(
    offer_cell: Scenario,
) -> None:
    await offer_cell.register_experiment()
    before = await offer_cell.store.export_company(COMPANY_ID)
    started = await offer_cell.start_experiment()
    after = await offer_cell.store.export_company(COMPANY_ID)

    experiment = _state_record(started, "experiment")
    assert experiment["state"] == "running"
    assert experiment["started_at"]
    group = _state_record(started, "reservation")
    members = _assert_mirrored_reservation_group(group, expected_minor=10_000)
    assert group["metadata"]["experiment_id"] == EXPERIMENT_ID
    assert len(after["reservations"]) == len(before["reservations"]) + 3
    exported_by_id = {
        reservation["reservation_id"]: reservation for reservation in after["reservations"]
    }
    for member in members:
        exported_member = exported_by_id[member["reservation_id"]]
        assert exported_member["reservation_group_id"] == group["reservation_group_id"]
        assert exported_member["ledger_role"] == member["ledger_role"]
        assert exported_member["budget_id"] == member["budget_id"]
        assert exported_member["status"] == member["status"]
        assert _money_number(exported_member["amount"]) == _money_number(member["amount"])

    before_budgets = {budget["budget_id"]: budget for budget in before["budgets"]}
    after_budgets = {budget["budget_id"]: budget for budget in after["budgets"]}
    logical_amount = _money_number(group["amount"])
    mirrored_deltas = []
    for budget_id in group["budget_ids"].values():
        delta = _money_number(after_budgets[budget_id]["reserved"]) - _money_number(
            before_budgets[budget_id]["reserved"]
        )
        assert delta == logical_amount
        mirrored_deltas.append(delta)
    assert sum(mirrored_deltas) == logical_amount * 3
    assert _money_number(group["amount"]) == logical_amount

    stage_filler_id = stable_uuid("stage-capacity-filler-experiment")
    await offer_cell.register_experiment(
        experiment_id=stage_filler_id,
        budget_minor=10_000,
        key="register-stage-capacity-filler-experiment",
    )
    filled_stage = await offer_cell.start_experiment(
        experiment_id=stage_filler_id,
        key="start-stage-capacity-filler-experiment",
    )
    filler_group = _state_record(filled_stage, "reservation")
    _assert_mirrored_reservation_group(filler_group, expected_minor=10_000)

    oversubscribed_id = stable_uuid("oversubscribed-stage-experiment")
    await offer_cell.register_experiment(
        experiment_id=oversubscribed_id,
        budget_minor=1_000,
        key="register-oversubscribed-stage-experiment",
    )
    before_rejection = await offer_cell.store.export_company(COMPANY_ID)
    stage_budget = next(
        budget
        for budget in before_rejection["budgets"]
        if budget["budget_id"] == filler_group["budget_ids"]["stage"]
    )
    assert stage_budget["reserved"] == stage_budget["limit"]
    with pytest.raises(BudgetError):
        await offer_cell.start_experiment(
            experiment_id=oversubscribed_id,
            key="reject-oversubscribed-stage-experiment",
        )
    after_rejection = await offer_cell.store.export_company(COMPANY_ID)
    assert after_rejection["reservations"] == before_rejection["reservations"]
    assert after_rejection["budgets"] == before_rejection["budgets"]
    assert not any(
        event["event_type"] == "experiment.started" and event["aggregate_id"] == oversubscribed_id
        for event in after_rejection["domain_events"]
    )


@pytest.mark.asyncio
async def test_ac08_starts_claims_and_completions_are_replay_safe(
    offer_cell: Scenario,
) -> None:
    registered = await offer_cell.register_experiment()
    experiment_revision = _state_record(registered, "experiment")["revision"]
    first_start = await offer_cell.start_experiment(
        expected_revision=experiment_revision,
        key="same-start",
    )
    after_start = await offer_cell.store.export_company(COMPANY_ID)
    replayed_start = await offer_cell.start_experiment(
        expected_revision=experiment_revision,
        key="same-start",
    )
    after_start_replay = await offer_cell.store.export_company(COMPANY_ID)
    _assert_replay(first_start, replayed_start)
    assert len(after_start_replay["reservations"]) == len(after_start["reservations"])
    assert len(after_start_replay["domain_events"]) == len(after_start["domain_events"])
    assert len(after_start_replay["audit_events"]) == len(after_start["audit_events"])
    with pytest.raises(ConflictError):
        await offer_cell.service.invoke(
            "experiment.start",
            offer_cell.principals["owner"],
            mutation(
                "experiment.start",
                {"experiment_id": stable_uuid("different-experiment")},
                expected_revision=experiment_revision,
                key="same-start",
            ),
        )

    await offer_cell.create_work()
    work_revision = offer_cell.work_revision
    first_claim = await offer_cell.claim_work(
        expected_revision=work_revision,
        key="same-claim",
    )
    replayed_claim = await offer_cell.claim_work(
        expected_revision=work_revision,
        key="same-claim",
    )
    _assert_replay(first_claim, replayed_claim)
    assert (
        _state_record(first_claim, "lease")["token"]
        == _state_record(replayed_claim, "lease")["token"]
    )

    await offer_cell.append_evidence()
    completion_revision = offer_cell.work_revision
    first_completion = await offer_cell.complete_work(key="same-completion")
    events_after_completion = (await offer_cell.store.export_company(COMPANY_ID))["domain_events"]
    lease = _state_record(first_claim, "lease")
    replayed_completion = await offer_cell.service.invoke(
        "work.complete",
        offer_cell.principals["specialist"],
        mutation(
            "work.complete",
            {
                "work_item_id": WORK_ITEM_ID,
                "lease_token": lease["token"],
                "result_evidence_id": EVIDENCE_ID,
                "actual_cost": {"currency": "USD", "amount_minor": 500},
            },
            expected_revision=completion_revision,
            key="same-completion",
            correlation="vertical-slice",
        ),
    )
    _assert_replay(first_completion, dict(replayed_completion))
    assert len((await offer_cell.store.export_company(COMPANY_ID))["domain_events"]) == len(
        events_after_completion
    )


def _assert_replay(first: dict[str, Any], second: dict[str, Any]) -> None:
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert {key: value for key, value in first.items() if key != "replayed"} == {
        key: value for key, value in second.items() if key != "replayed"
    }


@pytest.mark.asyncio
async def test_ac09_stale_expired_unauthorized_and_cross_tenant_calls_fail_closed(
    scenario: Scenario,
) -> None:
    await scenario.build_running_experiment()
    await scenario.create_work()
    claim = await scenario.claim_work(lease_seconds=5)
    lease = _state_record(claim, "lease")

    with pytest.raises(ConflictError):
        await scenario.service.invoke(
            "offer.mutate",
            scenario.principals["owner"],
            mutation(
                "offer.mutate",
                {
                    "parent_offer_version_id": OFFER_VERSION_ID,
                    "offer_version_id": stable_uuid("stale-child"),
                    "mutation_summary": "This caller did not re-read the parent.",
                    "changes": {"target_segment": "A stale segment"},
                },
                expected_revision=scenario.offer_revision + 1,
                key="stale-offer-mutation",
            ),
        )

    scenario.clock.advance(seconds=6)
    with pytest.raises(LeaseError):
        await scenario.service.invoke(
            "work.complete",
            scenario.principals["specialist"],
            mutation(
                "work.complete",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "lease_token": lease["token"],
                    "result_evidence_id": EVIDENCE_ID,
                    "actual_cost": {"currency": "USD", "amount_minor": 0},
                },
                expected_revision=scenario.work_revision,
                key="complete-after-expiry",
            ),
        )

    reclaimed = await scenario.claim_work(
        expected_revision=scenario.work_revision,
        key="reclaim-expired-work",
    )
    reclaimed_work = _state_record(reclaimed, "work_item")
    reclaimed_lease = _state_record(reclaimed, "lease")
    assert reclaimed_work["status"] == "claimed"
    assert reclaimed_lease["token"] != lease["token"]
    assert reclaimed_lease["acquired_at"] == scenario.clock().isoformat().replace("+00:00", "Z")

    with pytest.raises(AuthorizationError):
        await scenario.service.invoke(
            "allocation.commit",
            scenario.principals["specialist"],
            mutation(
                "allocation.commit",
                {"allocation_id": ALLOCATION_ID},
                expected_revision=1,
                key="specialist-cannot-allocate",
            ),
        )

    before_reserved_learning = await scenario.store.export_company(COMPANY_ID)
    for tool_name in ("learning.propose", "learning.promote"):
        with pytest.raises(AuthorizationError):
            await scenario.service.invoke(
                tool_name,
                scenario.principals["founder"],
                mutation(
                    tool_name,
                    {"learning_id": stable_uuid(f"unassigned-{tool_name}")},
                    expected_revision=0,
                    key=f"fail-closed-{tool_name}",
                ),
            )
    after_reserved_learning = await scenario.store.export_company(COMPANY_ID)
    assert len(after_reserved_learning["domain_events"]) == len(
        before_reserved_learning["domain_events"]
    )

    other_founder = make_principal(
        Role.FOUNDER,
        principal_id=stable_uuid("other-founder"),
        company_id=OTHER_COMPANY_ID,
    )
    await scenario.service.invoke(
        "company.bootstrap",
        other_founder,
        mutation(
            "company.bootstrap",
            {
                "company_id": OTHER_COMPANY_ID,
                "name": "Other company",
                "constitution": {
                    "schema_version": 1,
                    "constitution_id": stable_uuid("other-constitution"),
                    "version": 1,
                    "mission": "Remain isolated and prove tenant separation.",
                    "authority_policy": {
                        "founder_principal_ids": [stable_uuid("other-founder")],
                        "irreversible_actions": ["company.dissolve"],
                        "human_approval_actions": ["constitution.activate"],
                    },
                    "risk_policy": {
                        "prohibited_actions": ["claim.fabricate"],
                        "pricing_floor_minor": 0,
                        "max_external_messages_per_experiment": 0,
                        "regulated_market_entry_requires_founder": True,
                    },
                    "budget_policy": {
                        "currency": "USD",
                        "company_limit_minor": 1_000,
                        "cell_limit_minor": 1_000,
                        "experiment_limit_minor": 1_000,
                        "single_action_limit_minor": 1_000,
                        "exploration_share_bps": 2_000,
                    },
                    "fitness_policy": {
                        "rule_version": "fitness-v1",
                        "required_gates": list(REQUIRED_GATES),
                        "minimum_evidence_count": 1,
                        "minimum_customer_outcome_bps": 7_000,
                        "minimum_delivery_quality_bps": 8_000,
                        "allocation_cadence_days": 7,
                    },
                },
                "agents": [],
                "budget": {
                    "budget_id": stable_uuid("other-budget"),
                    "limit": "10.00",
                    "currency": "USD",
                },
            },
            expected_revision=0,
            key="bootstrap-other-company",
        ),
    )
    with pytest.raises((NotFoundError, AuthorizationError)):
        await scenario.service.invoke("cell.get", other_founder, {"cell_id": CELL_ID})

    first_export = await scenario.store.export_company(COMPANY_ID)
    other_export = await scenario.store.export_company(OTHER_COMPANY_ID)
    assert first_export["company_id"] == COMPANY_ID
    assert other_export["company_id"] == OTHER_COMPANY_ID
    assert not (
        {event["event_id"] for event in first_export["domain_events"]}
        & {event["event_id"] for event in other_export["domain_events"]}
    )


@pytest.mark.asyncio
async def test_ac10_evidence_preserves_verified_provenance_and_ignores_spoofing(
    running_experiment: Scenario,
) -> None:
    await running_experiment.create_work()
    await running_experiment.claim_work()
    response = await running_experiment.append_evidence()
    evidence = _state_record(response, "evidence")

    assert evidence["company_id"] == COMPANY_ID
    assert evidence["recorded_by_principal_id"] == PRINCIPAL_IDS["specialist"]
    assert evidence["agent_run_id"] == RUN_IDS["specialist"]
    assert evidence["source_system"] == "fake_crm"
    assert evidence["source_reference"] == "fake_crm:outcome:1"
    assert evidence["observed_at"]
    assert evidence["recorded_at"]
    assert evidence["confidence_bps"] == 9_000
    assert evidence["causal_distance"] == "direct"
    assert evidence["lineage"] == {
        "relation": "original",
        "related_evidence_id": None,
    }
    assert evidence["recorded_by_principal_id"] != stable_uuid("spoofed-principal")
    assert evidence["agent_run_id"] != stable_uuid("spoofed-run")


@pytest.mark.asyncio
async def test_ac10_work_completion_rejects_evidence_outside_its_experiment(
    running_experiment: Scenario,
) -> None:
    await running_experiment.create_work()
    claim = await running_experiment.claim_work()
    lease = _state_record(claim, "lease")
    unrelated_evidence_id = stable_uuid("evidence-without-work-experiment")
    await running_experiment.append_evidence(
        evidence_id=unrelated_evidence_id,
        experiment_id=None,
        key="append-evidence-outside-work-experiment",
    )
    before = await running_experiment.store.export_company(COMPANY_ID)

    with pytest.raises(ValidationError):
        await running_experiment.service.invoke(
            "work.complete",
            running_experiment.principals["specialist"],
            mutation(
                "work.complete",
                {
                    "work_item_id": WORK_ITEM_ID,
                    "lease_token": lease["token"],
                    "result_evidence_id": unrelated_evidence_id,
                    "actual_cost": {"currency": "USD", "amount_minor": 500},
                },
                expected_revision=running_experiment.work_revision,
                key="reject-unrelated-completion-evidence",
            ),
        )
    after = await running_experiment.store.export_company(COMPANY_ID)
    assert len(after["domain_events"]) == len(before["domain_events"])
    assert any(item["status"] == "active" for item in after["work_leases"])


@pytest.mark.asyncio
async def test_ac11_executor_cannot_be_the_experiments_evaluator(
    offer_cell: Scenario,
) -> None:
    self_evaluated_experiment = stable_uuid("self-evaluated-experiment")
    with pytest.raises(SeparationOfDutiesError):
        await offer_cell.register_experiment(
            experiment_id=self_evaluated_experiment,
            executors=[PRINCIPAL_IDS["evaluator"]],
        )

    exported = await offer_cell.store.export_company(COMPANY_ID)
    assert not any(
        projection["projection_type"] == "experiment"
        and projection["projection_id"] == self_evaluated_experiment
        for projection in exported["projections"]
    )


@pytest.mark.asyncio
async def test_ac12_independent_evaluator_closes_against_the_frozen_criteria(
    evaluating_experiment: Scenario,
) -> None:
    frozen = deepcopy(
        _state_record(
            evaluating_experiment.responses[f"experiment.register:{EXPERIMENT_ID}"],
            "experiment",
        )
    )
    experiment_revision = evaluating_experiment.experiment_revision()
    before = await evaluating_experiment.store.export_company(COMPANY_ID)
    invalid_results = (
        ("unfrozen-criterion", "invented_metric", 1),
        ("threshold-mismatch", "paid_pilot_requests", 0),
    )
    for label, criterion_id, observed_value in invalid_results:
        with pytest.raises(ValidationError):
            await evaluating_experiment.service.invoke(
                "evaluation.submit",
                evaluating_experiment.principals["evaluator"],
                mutation(
                    "evaluation.submit",
                    {
                        "evaluation_id": stable_uuid(f"{label}-evaluation"),
                        "experiment_id": EXPERIMENT_ID,
                        "experiment_revision": experiment_revision,
                        "evaluator_principal_id": stable_uuid("spoofed-evaluator"),
                        "evaluator_run_id": stable_uuid("spoofed-evaluator-run"),
                        "executor_principal_ids": [PRINCIPAL_IDS["specialist"]],
                        "window_ends_at": frozen["window"]["ends_at"],
                        "criterion_results": [
                            {
                                "criterion_id": criterion_id,
                                "outcome": "passed",
                                "observed_value": observed_value,
                                "evidence_ids": [EVIDENCE_ID],
                            }
                        ],
                        "result": "passed",
                        "summary": "Only the frozen threshold can close the experiment.",
                    },
                    expected_revision=experiment_revision,
                    key=f"reject-{label}",
                ),
            )
    after_rejection = await evaluating_experiment.store.export_company(COMPANY_ID)
    assert len(after_rejection["domain_events"]) == len(before["domain_events"])

    response = await evaluating_experiment.submit_evaluation(result="passed")
    evaluation = _state_record(response, "evaluation")
    experiment = _state_record(response, "experiment")

    assert evaluation["evaluator_principal_id"] == PRINCIPAL_IDS["evaluator"]
    assert evaluation["evaluator_run_id"] == RUN_IDS["evaluator"]
    assert evaluation["executor_principal_ids"] == [PRINCIPAL_IDS["specialist"]]
    assert evaluation["result"] == "passed"
    assert (
        evaluation["criterion_results"][0]["criterion_id"]
        == (frozen["success_thresholds"][0]["metric_id"])
    )
    assert evaluation["criterion_results"][0]["evidence_ids"] == [EVIDENCE_ID]
    assert experiment["state"] == "passed"


@pytest.mark.asyncio
async def test_ac13_complete_eligibility_gates_are_recorded_before_ranking(
    evaluated_experiment: Scenario,
) -> None:
    before = await evaluated_experiment.store.export_company(COMPANY_ID)
    with pytest.raises((ValidationError, NotFoundError, ConflictError)):
        await evaluated_experiment.invoke(
            "allocation.propose",
            "allocator",
            {
                "allocation_id": ALLOCATION_ID,
                "action": "fund",
                "target": {
                    "kind": "cell",
                    "id": CELL_ID,
                    "revision": evaluated_experiment.cell_revision,
                },
                "stage": "exploitation",
                "gate_decision_id": stable_uuid("missing-gate-decision"),
                "ranking": {
                    "formula": "exploitation-v1",
                    "input_snapshot": {
                        "expected_12m_contribution_profit_minor": 500_000,
                        "evidence_confidence_bps": 9_000,
                        "expected_repeatability_bps": 6_000,
                        "capital_at_risk_minor": 10_000,
                        "time_to_cash_days": 30,
                    },
                },
                "amount": {"currency": "USD", "amount_minor": 9_000},
                "rationale_summary": "This must fail because no gates exist.",
            },
            expected_revision=0,
            key="rank-before-gates",
        )
    after_rejection = await evaluated_experiment.store.export_company(COMPANY_ID)
    assert len(after_rejection["domain_events"]) == len(before["domain_events"])

    unrelated_cell_id = stable_uuid("unrelated-gate-target-cell")
    unrelated_owner_id = stable_uuid("unrelated-gate-target-owner")
    unrelated_owner_definition = deepcopy(load_definition("cell-owner"))
    unrelated_owner_definition.update(
        definition_id=stable_uuid("unrelated-gate-target-owner-definition"),
        name="unrelated-gate-target-owner",
    )
    await evaluated_experiment.invoke(
        "agent.register",
        "founder",
        {
            "principal_id": unrelated_owner_id,
            "cell_id": unrelated_cell_id,
            "definition": unrelated_owner_definition,
        },
        expected_revision=0,
        key="register-unrelated-gate-target-owner",
    )
    evaluated_experiment.principals["unrelated_owner"] = make_principal(
        Role.CELL_OWNER,
        principal_id=unrelated_owner_id,
        agent_run_id=stable_uuid("unrelated-gate-target-owner-run"),
    )
    unrelated_cell = await evaluated_experiment.invoke(
        "cell.create",
        "unrelated_owner",
        {
            "cell_id": unrelated_cell_id,
            "name": "Unrelated cell",
            "objective": "Prove evaluation lineage cannot cross cell boundaries.",
            "budget_id": stable_uuid("unrelated-gate-target-budget"),
            "budget_limit": "100.00",
            "currency": "USD",
        },
        expected_revision=0,
        key="create-unrelated-gate-target",
    )
    unrelated_allocator_id = stable_uuid("unrelated-gate-target-allocator")
    unrelated_allocator_definition = deepcopy(load_definition("allocator"))
    unrelated_allocator_definition.update(
        definition_id=stable_uuid("unrelated-gate-target-allocator-definition"),
        name="unrelated-gate-target-allocator",
    )
    await evaluated_experiment.invoke(
        "agent.register",
        "founder",
        {
            "principal_id": unrelated_allocator_id,
            "cell_id": unrelated_cell_id,
            "definition": unrelated_allocator_definition,
        },
        expected_revision=0,
        key="register-unrelated-gate-target-allocator",
    )
    evaluated_experiment.principals["unrelated_allocator"] = make_principal(
        Role.ALLOCATOR,
        principal_id=unrelated_allocator_id,
        agent_run_id=stable_uuid("unrelated-gate-target-allocator-run"),
    )
    before_cross_cell = await evaluated_experiment.store.export_company(COMPANY_ID)
    with pytest.raises(ValidationError):
        await evaluated_experiment.invoke(
            "gate.evaluate",
            "allocator",
            {
                "action": "fund",
                "stage": "exploitation",
                "target": {
                    "kind": "cell",
                    "id": unrelated_cell_id,
                    "revision": _state_record(unrelated_cell, "cell")["revision"],
                },
                "evaluation_id": EVALUATION_ID,
                "amount": {"currency": "USD", "amount_minor": 9_000},
            },
            expected_revision=0,
            key="reject-cross-cell-gate-evidence",
        )
    after_cross_cell = await evaluated_experiment.store.export_company(COMPANY_ID)
    assert len(after_cross_cell["domain_events"]) == len(before_cross_cell["domain_events"])

    before_cross_bound_gate = await evaluated_experiment.store.export_company(COMPANY_ID)
    with pytest.raises(AuthorizationError):
        await evaluated_experiment.evaluate_gates(
            actor="unrelated_allocator",
            key="reject-cross-bound-gate",
        )
    after_cross_bound_gate = await evaluated_experiment.store.export_company(COMPANY_ID)
    assert len(after_cross_bound_gate["domain_events"]) == len(
        before_cross_bound_gate["domain_events"]
    )

    gate_response = await evaluated_experiment.evaluate_gates()
    gate_state = gate_response["state"].get(
        "decision", gate_response["state"].get("gate_result", gate_response["state"])
    )
    gate_results = gate_state["gate_results"]
    assert [result["gate"] for result in gate_results] == list(REQUIRED_GATES)
    assert all(result["passed"] for result in gate_results)
    assert gate_state["amount"] == {"currency": "USD", "amount_minor": 9_000}

    after_gates = await evaluated_experiment.store.export_company(COMPANY_ID)
    with pytest.raises((ValidationError, ConflictError)):
        await evaluated_experiment.propose_allocation(
            allocation_id=stable_uuid("allocation-not-covered-by-gates"),
            amount_minor=50_000,
            key="reject-ungated-amount",
        )
    after_ungated_amount = await evaluated_experiment.store.export_company(COMPANY_ID)
    assert len(after_ungated_amount["domain_events"]) == len(after_gates["domain_events"])

    before_cross_bound_proposal = await evaluated_experiment.store.export_company(COMPANY_ID)
    with pytest.raises(AuthorizationError):
        await evaluated_experiment.propose_allocation(
            allocation_id=stable_uuid("cross-bound-allocation-proposal"),
            key="reject-cross-bound-allocation-proposal",
            actor="unrelated_allocator",
        )
    after_cross_bound_proposal = await evaluated_experiment.store.export_company(COMPANY_ID)
    assert len(after_cross_bound_proposal["domain_events"]) == len(
        before_cross_bound_proposal["domain_events"]
    )

    await evaluated_experiment.propose_allocation()
    before_cross_bound_commit = await evaluated_experiment.store.export_company(COMPANY_ID)
    with pytest.raises(AuthorizationError):
        await evaluated_experiment.commit_allocation(
            actor="unrelated_allocator",
            key="reject-cross-bound-allocation-commit",
        )
    after_cross_bound_commit = await evaluated_experiment.store.export_company(COMPANY_ID)
    assert len(after_cross_bound_commit["domain_events"]) == len(
        before_cross_bound_commit["domain_events"]
    )

    events = (await evaluated_experiment.store.export_company(COMPANY_ID))["domain_events"]
    gate_positions = [
        event["position"]
        for event in events
        if event["event_type"] in {"gate.evaluated", "decision.gates_evaluated"}
    ]
    allocation_positions = [
        event["position"] for event in events if event["event_type"] == "allocation.proposed"
    ]
    assert gate_positions and allocation_positions
    assert max(gate_positions) < min(allocation_positions)


@pytest.mark.asyncio
async def test_ac14_allocator_commit_records_exact_rule_constitution_and_authority(
    allocated_company: Scenario,
) -> None:
    response = allocated_company.responses["allocation.commit"]
    allocation = _state_record(response, "allocation")
    reservation_group = _state_record(response, "reservation")
    _assert_mirrored_reservation_group(reservation_group, expected_minor=9_000)

    assert allocation["decision_state"] == "committed"
    assert allocation["allocation_id"] == ALLOCATION_ID
    assert allocation["rule_version"] == "fitness-v1"
    assert allocation["constitution_id"] == CONSTITUTION_ID
    assert allocation["constitution_version"] == 1
    assert allocation["amount"] == {"currency": "USD", "amount_minor": 9_000}
    assert allocation["proposed_by_principal_id"] == PRINCIPAL_IDS["allocator"]
    assert allocation["proposed_by_run_id"] == RUN_IDS["allocator"]
    assert allocation["decided_by_principal_id"] == PRINCIPAL_IDS["allocator"]
    assert allocation["decided_by_run_id"] == RUN_IDS["allocator"]
    assert allocation["ranking"] == {
        "formula": "exploitation-v1",
        "score_numerator": 500_000 * 9_000 * 6_000 * 30,
        "score_denominator": 10_000 * 30 * 10_000 * 10_000,
        "input_snapshot": {
            "expected_12m_contribution_profit_minor": 500_000,
            "evidence_confidence_bps": 9_000,
            "expected_repeatability_bps": 6_000,
            "capital_at_risk_minor": 10_000,
            "time_to_cash_days": 30,
        },
    }
    assert len(allocation["input_digest"]) == 64
    assert all(item["passed"] for item in allocation["gate_results"])

    exported = await allocated_company.store.export_company(COMPANY_ID)
    budgets = {budget["budget_id"]: budget for budget in exported["budgets"]}
    assert budgets[reservation_group["budget_ids"]["company"]]["scope"] == "company"
    assert budgets[reservation_group["budget_ids"]["cell"]]["scope"] == "cell"
    stage_budget = budgets[reservation_group["budget_ids"]["stage"]]
    assert stage_budget["scope"] == "stage"
    assert stage_budget["stage"] == "exploitation"


@pytest.mark.asyncio
async def test_ac14_allocation_commit_rechecks_live_capacity_after_proposal() -> None:
    scenario = Scenario()
    await scenario.build_evaluated_experiment()
    gate_response = await scenario.evaluate_gates(key="evaluate-gates-before-capacity-race")
    proposal_response = await scenario.propose_allocation(key="propose-before-capacity-race")
    gate_decision = _state_record(gate_response, "decision")
    ledger_snapshots = {
        role: gate_decision["budget_snapshot"][role] for role in ("company", "cell", "stage")
    }

    async with scenario.store.transaction(COMPANY_ID) as transaction:
        competing_group = await transaction.reserve_budget_group(
            reservation_group_id=stable_uuid("competing-allocation-reservation-group"),
            budget_ids={role: snapshot["budget_id"] for role, snapshot in ledger_snapshots.items()},
            amount="410.00",
            purpose="concurrent permitted action won the live capacity race",
            expected_revisions={
                snapshot["budget_id"]: snapshot["revision"]
                for snapshot in ledger_snapshots.values()
            },
            idempotency_key="competing-live-capacity-reservation",
            metadata={"stage": "exploitation"},
            now=scenario.clock(),
        )
    _assert_mirrored_reservation_group(competing_group, expected_minor=41_000)

    before_rejection = await scenario.store.export_company(COMPANY_ID)
    with pytest.raises(BudgetError):
        await scenario.commit_allocation(key="reject-stale-capacity-allocation-commit")
    after_rejection = await scenario.store.export_company(COMPANY_ID)

    assert after_rejection["budgets"] == before_rejection["budgets"]
    assert after_rejection["reservations"] == before_rejection["reservations"]
    assert after_rejection["domain_events"] == before_rejection["domain_events"]
    assert after_rejection["audit_events"] == before_rejection["audit_events"]
    assert after_rejection["idempotency_records"] == before_rejection["idempotency_records"]
    persisted_allocation = next(
        projection["state"]
        for projection in after_rejection["projections"]
        if projection["projection_type"] == "allocation"
        and projection["projection_id"] == ALLOCATION_ID
    )
    assert persisted_allocation == _state_record(proposal_response, "allocation")
    assert persisted_allocation["decision_state"] == "proposed"
    assert not any(
        event["event_type"] == "allocation.committed" for event in after_rejection["domain_events"]
    )


@pytest.mark.asyncio
async def test_non_funding_allocation_commits_without_a_budget_hold(
    evaluated_experiment: Scenario,
) -> None:
    target = {
        "kind": "cell",
        "id": CELL_ID,
        "revision": evaluated_experiment.cell_revision,
    }
    gate_response = await evaluated_experiment.invoke(
        "gate.evaluate",
        "allocator",
        {
            "action": "maintain",
            "stage": "exploitation",
            "target": target,
            "evaluation_id": EVALUATION_ID,
            "amount": None,
        },
        expected_revision=0,
        key="evaluate-maintain-without-funding",
    )
    gate = _state_record(gate_response, "decision")
    allocation_id = stable_uuid("maintain-without-funding")
    proposal_response = await evaluated_experiment.invoke(
        "allocation.propose",
        "allocator",
        {
            "allocation_id": allocation_id,
            "action": "maintain",
            "target": target,
            "stage": "exploitation",
            "gate_decision_id": gate["decision_id"],
            "ranking": {
                "input_snapshot": {
                    "expected_12m_contribution_profit_minor": 500_000,
                    "evidence_confidence_bps": 9_000,
                    "expected_repeatability_bps": 6_000,
                    "capital_at_risk_minor": 10_000,
                    "time_to_cash_days": 30,
                }
            },
            "amount": None,
            "rationale_summary": "Maintain the current cell without new capital.",
        },
        expected_revision=0,
        key="propose-maintain-without-funding",
    )
    proposed = _state_record(proposal_response, "allocation")
    committed_response = await evaluated_experiment.invoke(
        "allocation.commit",
        "allocator",
        {"allocation_id": allocation_id},
        expected_revision=proposed["revision"],
        key="commit-maintain-without-funding",
    )
    committed = _state_record(committed_response, "allocation")
    assert committed["decision_state"] == "committed"
    assert committed["amount"] is None
    assert committed_response["state"]["reservation"] is None


@pytest.mark.asyncio
async def test_ac15_budget_and_policy_violations_block_and_contain_side_effects() -> None:
    budget_case = Scenario()
    await budget_case.build_offer_cell()
    oversized_id = stable_uuid("oversized-experiment")
    registered = await budget_case.register_experiment(
        experiment_id=oversized_id,
        budget_minor=100_000_000,
    )
    revision = _state_record(registered, "experiment")["revision"]
    before = await budget_case.store.export_company(COMPANY_ID)
    with pytest.raises(BudgetError):
        await budget_case.start_experiment(
            experiment_id=oversized_id,
            expected_revision=revision,
            key="start-over-budget",
        )
    after = await budget_case.store.export_company(COMPANY_ID)
    assert after["reservations"] == before["reservations"]
    assert not any(
        event["event_type"] == "experiment.started" and event["aggregate_id"] == oversized_id
        for event in after["domain_events"]
    )

    policy_case = Scenario()
    await policy_case.build_running_experiment()
    await policy_case.create_work()
    await policy_case.claim_work()
    await policy_case.append_evidence(
        evidence_id=stable_uuid("policy-violation-evidence"),
        evidence_type="policy_violation",
        key="append-policy-violation",
    )
    cell = await policy_case.service.invoke(
        "cell.get", policy_case.principals["immune"], {"cell_id": CELL_ID}
    )
    assert cell["cell"]["status"] == "paused"

    with pytest.raises((ValidationError, AuthorizationError, LeaseError)):
        await policy_case.complete_work(key="complete-after-policy-stop")


@pytest.mark.asyncio
async def test_ac15_founder_authorized_containment_rejects_spoofing_and_commits_pause() -> None:
    containment = Scenario()
    await containment.build_offer_cell()
    target = {
        "kind": "cell",
        "id": CELL_ID,
        "revision": containment.cell_revision,
    }

    before_invalid = await containment.store.export_company(COMPANY_ID)
    invalid_bases: tuple[dict[str, str] | None, ...] = (
        None,
        {"kind": "unrelated", "summary": "This is not founder authority."},
        {
            "kind": "policy_violation",
            "summary": "A caller assertion is not a recorded policy basis.",
        },
        {
            "kind": "stop_rule",
            "summary": "A caller assertion is not a recorded stop-rule basis.",
        },
    )
    for index, basis in enumerate(invalid_bases):
        payload: dict[str, Any] = {
            "action": "pause",
            "stage": "containment",
            "target": target,
            "amount": None,
        }
        if basis is not None:
            payload["containment_basis"] = basis
        with pytest.raises(ValidationError):
            await containment.service.invoke(
                "gate.evaluate",
                containment.principals["founder"],
                mutation(
                    "gate.evaluate",
                    payload,
                    expected_revision=0,
                    key=f"reject-invalid-containment-basis-{index}",
                ),
            )
    for action in ("pause", "kill"):
        with pytest.raises(AuthorizationError):
            await containment.service.invoke(
                "gate.evaluate",
                containment.principals["owner"],
                mutation(
                    "gate.evaluate",
                    {
                        "action": action,
                        "stage": "containment",
                        "target": target,
                        "amount": None,
                        "containment_basis": {
                            "kind": "founder_authorized",
                            "summary": "An owner cannot spoof founder authorization.",
                        },
                    },
                    expected_revision=0,
                    key=f"reject-non-founder-containment-{action}",
                ),
            )
    after_invalid = await containment.store.export_company(COMPANY_ID)
    assert len(after_invalid["domain_events"]) == len(before_invalid["domain_events"])
    assert len(after_invalid["audit_events"]) == len(before_invalid["audit_events"])

    gate_response = await containment.invoke(
        "gate.evaluate",
        "founder",
        {
            "action": "pause",
            "stage": "containment",
            "target": target,
            "amount": None,
            "containment_basis": {
                "kind": "founder_authorized",
                "summary": "Pause external side effects while the founder reviews risk.",
            },
        },
        expected_revision=0,
        key="founder-authorized-containment-gate",
    )
    decision = _state_record(gate_response, "decision")
    assert decision["passed"] is True
    assert decision["amount"] is None
    assert decision["evaluation_id"] is None
    assert decision["experiment_id"] is None
    assert [result["gate"] for result in decision["gate_results"]] == list(REQUIRED_GATES)
    passed_by_gate = {result["gate"]: result["passed"] for result in decision["gate_results"]}
    assert passed_by_gate["customer_outcome_floor"] is False
    assert passed_by_gate["delivery_quality_floor"] is False
    assert passed_by_gate["data_sufficiency_and_independence"] is False
    assert decision["containment_basis"]["kind"] == "founder_authorized"
    assert decision["containment_basis"]["authorized_by_principal_id"] == PRINCIPAL_IDS["founder"]
    assert decision["containment_basis"]["authorized_at"]

    containment_allocation_id = stable_uuid("founder-authorized-pause-allocation")
    proposal = await containment.invoke(
        "allocation.propose",
        "allocator",
        {
            "allocation_id": containment_allocation_id,
            "action": "pause",
            "target": target,
            "stage": "containment",
            "gate_decision_id": decision["decision_id"],
            "ranking": {},
            "amount": None,
            "rationale_summary": "Execute the founder-authorized containment decision.",
        },
        expected_revision=0,
        key="propose-founder-authorized-pause",
    )
    proposed = _state_record(proposal, "allocation")
    assert proposed["ranking"] == {
        "formula": "not-ranked",
        "score_numerator": 0,
        "score_denominator": 1,
        "input_snapshot": {},
    }

    committed = await containment.invoke(
        "allocation.commit",
        "allocator",
        {"allocation_id": containment_allocation_id},
        expected_revision=proposed["revision"],
        key="commit-founder-authorized-pause",
    )
    allocation = _state_record(committed, "allocation")
    cell = _state_record(committed, "cell")
    assert allocation["decision_state"] == "committed"
    assert allocation["amount"] is None
    assert committed["state"]["reservation"] is None
    assert cell["status"] == "paused"
    assert cell["revision"] == target["revision"] + 1

    kill_target = {
        "kind": "cell",
        "id": CELL_ID,
        "revision": cell["revision"],
    }
    kill_gate_response = await containment.invoke(
        "gate.evaluate",
        "founder",
        {
            "action": "kill",
            "stage": "containment",
            "target": kill_target,
            "amount": None,
            "containment_basis": {
                "kind": "founder_authorized",
                "summary": "The founder confirms that permanent containment is required.",
            },
        },
        expected_revision=0,
        key="founder-authorized-kill-gate",
    )
    kill_gate = _state_record(kill_gate_response, "decision")
    kill_allocation_id = stable_uuid("founder-authorized-kill-allocation")
    kill_proposal_response = await containment.invoke(
        "allocation.propose",
        "allocator",
        {
            "allocation_id": kill_allocation_id,
            "action": "kill",
            "target": kill_target,
            "stage": "containment",
            "gate_decision_id": kill_gate["decision_id"],
            "ranking": {},
            "amount": None,
            "rationale_summary": "Execute the founder-authorized irreversible containment.",
        },
        expected_revision=0,
        key="propose-founder-authorized-kill",
    )
    kill_proposal = _state_record(kill_proposal_response, "allocation")

    before_allocator_rejection = await containment.store.export_company(COMPANY_ID)
    with pytest.raises(AuthorizationError):
        await containment.service.invoke(
            "allocation.commit",
            containment.principals["allocator"],
            mutation(
                "allocation.commit",
                {"allocation_id": kill_allocation_id},
                expected_revision=kill_proposal["revision"],
                key="reject-allocator-irreversible-commit",
            ),
        )
    after_allocator_rejection = await containment.store.export_company(COMPANY_ID)
    assert after_allocator_rejection["domain_events"] == before_allocator_rejection["domain_events"]
    assert after_allocator_rejection["audit_events"] == before_allocator_rejection["audit_events"]

    containment.principals["human_founder"] = make_principal(
        Role.FOUNDER,
        principal_id=PRINCIPAL_IDS["founder"],
    )
    killed_response = await containment.invoke(
        "allocation.commit",
        "human_founder",
        {"allocation_id": kill_allocation_id},
        expected_revision=kill_proposal["revision"],
        key="founder-commits-irreversible-kill",
    )
    killed_allocation = _state_record(killed_response, "allocation")
    killed_cell = _state_record(killed_response, "cell")
    assert killed_allocation["decision_state"] == "committed"
    assert killed_allocation["decided_by_principal_id"] == PRINCIPAL_IDS["founder"]
    assert killed_allocation["decided_by_run_id"] is None
    assert killed_response["state"]["reservation"] is None
    assert killed_cell["status"] == "killed"
    assert killed_cell["revision"] == kill_target["revision"] + 1


@pytest.mark.asyncio
async def test_ac16_every_decision_and_agent_run_is_reconstructable_without_reasoning(
    allocated_company: Scenario,
) -> None:
    oversized_definition = deepcopy(load_definition("cell-owner"))
    oversized_definition.update(
        definition_id=stable_uuid("oversized-event-agent-definition"),
        name="oversized-event-agent-definition",
        instructions=["x" * 1_000 for _ in range(50)],
    )
    before_oversized_event = await allocated_company.store.export_company(COMPANY_ID)
    with pytest.raises(ValidationError):
        await allocated_company.service.invoke(
            "agent.register",
            allocated_company.principals["founder"],
            mutation(
                "agent.register",
                {
                    "principal_id": stable_uuid("oversized-event-agent-principal"),
                    "definition": oversized_definition,
                },
                expected_revision=0,
                key="reject-oversized-domain-event",
            ),
        )
    after_oversized_event = await allocated_company.store.export_company(COMPANY_ID)
    assert len(after_oversized_event["domain_events"]) == len(
        before_oversized_event["domain_events"]
    )
    assert not any(
        projection["projection_id"] == stable_uuid("oversized-event-agent-definition")
        for projection in after_oversized_event["projections"]
    )

    explained = await allocated_company.service.invoke(
        "decision.explain",
        allocated_company.principals["founder"],
        {"decision_id": ALLOCATION_ID},
    )
    decision = explained["decision"]
    assert decision["constitution_id"] == CONSTITUTION_ID
    assert decision["rule_version"] == "fitness-v1"
    assert decision["gate_results"]
    assert decision["ranking"]["input_snapshot"]
    assert decision["evidence_ids"]
    assert_no_hidden_reasoning(decision)

    audit = await allocated_company.service.invoke(
        "audit.list", allocated_company.principals["founder"], {"limit": 1_000}
    )
    assert audit["audit_events"]
    assert_no_hidden_reasoning(audit)

    exported = await allocated_company.store.export_company(COMPANY_ID)
    by_run: dict[str, list[dict[str, Any]]] = {}
    for event in exported["domain_events"]:
        run_id = event.get("agent_run_id") or event.get("metadata", {}).get("agent_run_id")
        if run_id:
            by_run.setdefault(run_id, []).append(event)
    for actor in ("owner", "specialist", "evaluator", "allocator"):
        run_id = RUN_IDS[actor]
        assert by_run.get(run_id), f"no reconstructable events for {actor} run {run_id}"
        assert all(
            event.get("metadata", {}).get("agent_definition_version")
            or event.get("agent_definition_version")
            for event in by_run[run_id]
        )
    assert_no_hidden_reasoning(exported)


@pytest.mark.asyncio
async def test_ac17_event_replay_is_ordered_complete_and_projection_rebuildable(
    allocated_company: Scenario,
) -> None:
    exported = await allocated_company.store.export_company(COMPANY_ID)

    def collect(state: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
        return [*state, event]

    replayed = await allocated_company.store.replay(COMPANY_ID, collect, [])
    replayed_again = await allocated_company.store.replay(COMPANY_ID, collect, [])
    assert replayed == exported["domain_events"]
    assert replayed_again == replayed
    assert [event["position"] for event in replayed] == list(range(1, len(replayed) + 1))

    stream_revisions: dict[tuple[str, str], list[int]] = {}
    latest_states: dict[tuple[str, str], dict[str, Any]] = {}
    for event in replayed:
        stream = (event["aggregate_type"], event["aggregate_id"])
        stream_revisions.setdefault(stream, []).append(event["aggregate_revision"])
        state = event["payload"].get("state")
        if isinstance(state, dict):
            latest_states[stream] = state
    for revisions in stream_revisions.values():
        assert revisions == list(range(1, len(revisions) + 1))

    matched_projection_types: set[str] = set()
    for projection in exported["projections"]:
        stream = (projection["projection_type"], projection["projection_id"])
        if stream in latest_states:
            assert projection["state"] == latest_states[stream]
            matched_projection_types.add(projection["projection_type"])
    assert {"company", "cell", "experiment", "work_item", "evidence", "allocation"} <= (
        matched_projection_types
    )


@pytest.mark.asyncio
async def test_ac18_external_agent_runner_completes_the_slice_over_mcp() -> None:
    from ai_nature_company.runner import run_vertical_slice

    receipt = await run_vertical_slice()

    assert receipt["transport"] == "mcp"
    assert receipt["company_id"]
    assert receipt["allocation_id"]
    assert receipt["decision_state"] == "committed"
    assert {"owner", "specialist", "evaluator", "allocator"} <= set(receipt["agent_run_ids"])
    assert_no_hidden_reasoning(receipt)

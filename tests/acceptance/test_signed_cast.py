"""Signed seven-agent cast and four cell routines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_nature_company.auth import Role
from ai_nature_company.cast import CELL_ROUTINES, SIGNED_CAST, signed_definition_names
from ai_nature_company.errors import AuthorizationError, ValidationError
from ai_nature_company.experiment_stop import WINDOW_ELAPSED

from .scenario import REQUIRED_GATES, Scenario
from .support import (
    AGENTS_ROOT,
    CELL_ID,
    COMPANY_BUDGET_ID,
    COMPANY_ID,
    CONSTITUTION_ID,
    EVALUATION_ID,
    EVIDENCE_ID,
    EXPERIMENT_ID,
    MUTATED_OFFER_VERSION_ID,
    PRINCIPAL_IDS,
    load_definition,
    make_principal,
    mutation,
    stable_uuid,
)

SIGNED_DIRECTORIES = {
    "allocator": "allocator",
    "cell-owner": "cell_owner",
    "delivery": "specialist",
    "evaluator": "evaluator",
    "market-scout": "specialist",
    "memory": "specialist",
    "sales": "specialist",
}


def test_signed_definitions_exist_and_map_to_compatible_roles() -> None:
    present = {path.parent.name for path in AGENTS_ROOT.glob("*/agent-definition.json")}
    assert signed_definition_names() == set(SIGNED_DIRECTORIES)
    assert present >= signed_definition_names()
    assert "immune" not in signed_definition_names()
    assert "keeper" not in present
    assert "strategy" not in present

    for directory, role in SIGNED_DIRECTORIES.items():
        definition = load_definition(directory)
        spec = SIGNED_CAST[directory]
        assert definition["compatible_roles"] == [role]
        assert spec["compatible_role"] == role
        assert definition["constraints"]["may_self_evaluate"] is False
        assert "model" not in json.dumps(definition)


@pytest.mark.asyncio
async def test_protocol_describe_exposes_executable_routines() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["owner"],
        {},
    )
    assert described["inference_inside_substrate"] is False
    assert described["agent_scheduler_inside_substrate"] is False
    assert described["runtime_tick"]["not"] == "reflex"
    assert described["runtime_tick"]["forces"] == ["maintain", "continue"]
    assert "overdue_open_work" in described["runtime_tick"]["advances"]
    assert described["natural_forces"]["names"] == [
        "bound",
        "sense",
        "act",
        "transform",
        "maintain",
        "defend",
        "select",
        "continue",
    ]
    assert described["signed_cast_are"] == "optional_effectors_inside_forces"
    assert described["signed_cast"] == [
        "market-scout",
        "offer-designer",
        "sales",
        "delivery",
        "experiment-evaluator",
        "memory",
        "allocator",
    ]
    assert described["cell_routines"]["reflex"]["controller"] == "leased_work"
    assert described["cell_routines"]["adaptation"]["price_floor_enforced"] is True
    assert described["cell_routines"]["selection"]["controller"] == "allocator"
    assert described["cell_routines"]["selection"]["tick"] == "stop_rule_closeout_not_reflex"
    assert described["cell_routines"]["evolution"]["controller"] == "founder"
    assert "constitution.amend" in described["tools"]
    assert described["cell_routines"] == {name: dict(spec) for name, spec in CELL_ROUTINES.items()}


async def _register_labeled_specialist(scenario: Scenario, kind: str, actor: str) -> None:
    principal_id = stable_uuid(f"principal-{actor}")
    scenario.principals[actor] = make_principal(
        Role.SPECIALIST,
        principal_id=principal_id,
        agent_run_id=stable_uuid(f"run-{actor}"),
    )
    await scenario.invoke(
        "agent.register",
        "founder",
        {
            "principal_id": principal_id,
            "cell_id": CELL_ID,
            "definition": load_definition(kind),
        },
        expected_revision=0,
        key=f"register-{kind}",
    )


@pytest.mark.asyncio
async def test_scout_sales_and_delivery_cannot_evaluate() -> None:
    scenario = Scenario()
    await scenario.build_evaluating_experiment()
    for kind, actor in (
        ("market-scout", "scout"),
        ("sales", "sales"),
        ("delivery", "delivery"),
    ):
        await _register_labeled_specialist(scenario, kind, actor)
        definition = load_definition(kind)
        assert "evaluation.submit" not in definition["declared_capabilities"]
        with pytest.raises(AuthorizationError):
            await scenario.service.invoke(
                "evaluation.submit",
                scenario.principals[actor],
                mutation(
                    "evaluation.submit",
                    {
                        "evaluation_id": stable_uuid(f"eval-{actor}"),
                        "experiment_id": EXPERIMENT_ID,
                        "experiment_revision": scenario.experiment_revision(),
                        "evaluator_principal_id": PRINCIPAL_IDS["evaluator"],
                        "evaluator_run_id": stable_uuid(f"spoofed-{actor}-run"),
                        "executor_principal_ids": [PRINCIPAL_IDS["specialist"]],
                        "criterion_results": [
                            {
                                "criterion_id": "paid_pilot_requests",
                                "outcome": "passed",
                                "observed_value": 1,
                                "evidence_ids": [EVIDENCE_ID],
                            }
                        ],
                        "result": "passed",
                        "summary": "An executor cannot evaluate its own experiment.",
                    },
                    expected_revision=scenario.experiment_revision(),
                    key=f"{actor}-cannot-evaluate",
                ),
            )


@pytest.mark.asyncio
async def test_reflex_work_rejects_evaluation_capabilities() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    with pytest.raises(ValidationError, match="reflex work"):
        await scenario.invoke(
            "work.create",
            "owner",
            {
                "work_item_id": stable_uuid("reflex-eval-work"),
                "cell_id": CELL_ID,
                "experiment_id": EXPERIMENT_ID,
                "kind": "scout_market",
                "outcome_contract": {
                    "outcome": "Find one demand hint.",
                    "acceptance_criteria": ["Evidence cites a customer statement."],
                    "result_schema_ref": "schemas/evidence-v1.json",
                },
                "eligible_capabilities": ["evidence.append", "evaluation.submit"],
                "budget": {"currency": "USD", "maximum_cost_minor": 1_000},
                "approval_requirements": ["none"],
                "input_references": [],
                "max_attempts": 2,
            },
            expected_revision=0,
            key="reject-reflex-evaluation",
        )

    created = await scenario.invoke(
        "work.create",
        "owner",
        {
            "work_item_id": stable_uuid("reflex-scout-work"),
            "cell_id": CELL_ID,
            "experiment_id": EXPERIMENT_ID,
            "kind": "scout_market",
            "outcome_contract": {
                "outcome": "Find one demand hint.",
                "acceptance_criteria": ["Evidence cites a customer statement."],
                "result_schema_ref": "schemas/evidence-v1.json",
            },
            "eligible_capabilities": ["evidence.append"],
            "budget": {"currency": "USD", "maximum_cost_minor": 1_000},
            "approval_requirements": ["none"],
            "input_references": [],
            "max_attempts": 2,
        },
        expected_revision=0,
        key="create-reflex-scout-work",
    )
    assert created["state"]["work_item"]["kind"] == "scout_market"

    await _register_labeled_specialist(scenario, "market-scout", "scout")
    claimed = await scenario.invoke(
        "work.claim",
        "scout",
        {"work_item_id": stable_uuid("reflex-scout-work"), "lease_seconds": 300},
        expected_revision=1,
        key="scout-claims-reflex-work",
    )
    assert claimed["state"]["work_item"]["status"] == "claimed"


@pytest.mark.asyncio
async def test_adaptation_enforces_price_floor_on_immutable_child() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    before = await scenario.store.export_company(COMPANY_ID)
    with pytest.raises(ValidationError, match="pricing floor"):
        await scenario.invoke(
            "offer.mutate",
            "owner",
            {
                "parent_offer_version_id": MUTATED_OFFER_VERSION_ID,
                "mutation_summary": "Cut price below the constitution floor.",
                "changes": {"price": {"currency": "USD", "amount_minor": 1}},
            },
            expected_revision=1,
            key="reject-price-floor-cut",
        )
    after = await scenario.store.export_company(COMPANY_ID)
    assert len(after["domain_events"]) == len(before["domain_events"])


@pytest.mark.asyncio
async def test_allocator_can_propose_and_commit_bounded_allocation() -> None:
    scenario = Scenario()
    await scenario.build_evaluated_experiment()
    gate = await scenario.evaluate_gates()
    decision = gate["state"]["decision"]
    assert decision["passed"] is True
    assert {item["gate"] for item in decision["gate_results"]} == set(REQUIRED_GATES)
    proposed = await scenario.propose_allocation()
    committed = await scenario.commit_allocation()
    allocation = committed["state"]["allocation"]
    assert proposed["state"]["allocation"]["decision_state"] == "proposed"
    assert allocation["decision_state"] == "committed"
    assert allocation["action"] == "fund"
    assert allocation["decided_by_principal_id"] == PRINCIPAL_IDS["allocator"]
    assert allocation["rule_version"] == "fitness-v1"


@pytest.mark.asyncio
async def test_memory_can_propose_but_cannot_self_promote() -> None:
    scenario = Scenario()
    await scenario.build_evaluated_experiment()
    await _register_labeled_specialist(scenario, "memory", "memory")
    learning_id = stable_uuid("memory-candidate")
    proposed = await scenario.invoke(
        "learning.propose",
        "memory",
        {
            "learning_id": learning_id,
            "cell_id": CELL_ID,
            "source_experiment_id": EXPERIMENT_ID,
            "source_evaluation_id": EVALUATION_ID,
            "statement": "Proof-led samples produced a paid-pilot request at lower cost.",
            "confidence_bps": 7_500,
            "evidence_ids": [EVIDENCE_ID],
        },
        expected_revision=0,
        key="memory-propose-candidate",
    )
    candidate = proposed["state"]["learning"]
    assert candidate["status"] == "candidate"
    assert candidate["proposed_by_principal_id"] == scenario.principals["memory"].principal_id
    assert "learning.promote" not in load_definition("memory")["declared_capabilities"]

    before = await scenario.store.export_company(COMPANY_ID)
    with pytest.raises(AuthorizationError):
        await scenario.service.invoke(
            "learning.promote",
            scenario.principals["memory"],
            mutation(
                "learning.promote",
                {"learning_id": learning_id},
                expected_revision=1,
                key="memory-cannot-self-promote",
            ),
        )
    with pytest.raises(AuthorizationError, match="promotion gate is incomplete"):
        await scenario.service.invoke(
            "learning.promote",
            scenario.principals["founder"],
            mutation(
                "learning.promote",
                {"learning_id": learning_id},
                expected_revision=1,
                key="founder-promote-still-gated",
            ),
        )
    after = await scenario.store.export_company(COMPANY_ID)
    assert len(after["domain_events"]) == len(before["domain_events"])
    stored = next(
        projection
        for projection in after["projections"]
        if projection["projection_type"] == "learning"
        and projection["projection_id"] == learning_id
    )
    assert stored["state"]["status"] == "candidate"


@pytest.mark.asyncio
async def test_tick_still_closes_due_experiments_and_is_not_reflex() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    scenario.clock.advance(days=8)
    result = await scenario.service.tick(COMPANY_ID)
    assert result["inference_inside_substrate"] is False
    assert result["closed_experiment_ids"] == [EXPERIMENT_ID]
    assert result["closeout_work_item_ids"]
    exported = await scenario.store.export_company(COMPANY_ID)
    closeout = [
        projection["state"]
        for projection in exported["projections"]
        if projection["projection_type"] == "work_item"
        and projection["state"].get("kind") == "evaluate_experiment"
    ]
    reflex = [
        projection["state"]
        for projection in exported["projections"]
        if projection["projection_type"] == "work_item"
        and projection["state"].get("kind") in {"scout_market", "sales_step", "delivery_step"}
    ]
    assert closeout
    assert "evaluation.submit" in closeout[0]["eligible_capabilities"]
    assert reflex == []
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    due = next(item for item in viewed["experiments"] if item["experiment_id"] == EXPERIMENT_ID)
    assert due["state"] == "evaluating"
    assert WINDOW_ELAPSED in {rule["rule_id"] for rule in due["fired_rules"]}


@pytest.mark.asyncio
async def test_owner_led_path_works_without_an_allocator_principal() -> None:
    scenario = Scenario()
    owner_definition = load_definition("cell-owner")
    specialist_definition = load_definition("specialist")
    evaluator_definition = load_definition("evaluator")
    await scenario.invoke(
        "company.bootstrap",
        "founder",
        {
            "company_id": COMPANY_ID,
            "name": "Owner-led company without allocator",
            "constitution": {
                "schema_version": 1,
                "constitution_id": CONSTITUTION_ID,
                "version": 1,
                "mission": "Create verified customer value.",
                "authority_policy": {
                    "founder_principal_ids": [PRINCIPAL_IDS["founder"]],
                    "irreversible_actions": ["company.dissolve"],
                    "human_approval_actions": ["constitution.activate"],
                },
                "risk_policy": {
                    "prohibited_actions": ["claim.fabricate"],
                    "pricing_floor_minor": 10_000,
                    "max_external_messages_per_experiment": 10,
                    "regulated_market_entry_requires_founder": True,
                },
                "budget_policy": {
                    "unit": "money",
                    "currency": "USD",
                    "company_limit_minor": 100_000,
                    "cell_limit_minor": 50_000,
                    "experiment_limit_minor": 10_000,
                    "single_action_limit_minor": 10_000,
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
            "agents": [
                {
                    "principal_id": PRINCIPAL_IDS["owner"],
                    "cell_id": CELL_ID,
                    "definition": owner_definition,
                },
                {
                    "principal_id": PRINCIPAL_IDS["specialist"],
                    "cell_id": CELL_ID,
                    "definition": specialist_definition,
                },
                {
                    "principal_id": PRINCIPAL_IDS["evaluator"],
                    "cell_id": CELL_ID,
                    "definition": evaluator_definition,
                },
            ],
            "budget": {
                "budget_id": COMPANY_BUDGET_ID,
                "limit": "1000.00",
                "duration_days": 365,
                "currency": "USD",
            },
        },
        expected_revision=0,
        key="bootstrap-without-allocator",
    )
    scenario.responses["bootstrap"] = scenario.responses[
        "company.bootstrap:bootstrap-without-allocator"
    ]
    await scenario.create_cell()
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()
    paused = await scenario.invoke(
        "cell.transition",
        "owner",
        {"cell_id": CELL_ID, "target_status": "paused"},
        expected_revision=scenario.cell_revision,
        key="owner-pause-without-allocator",
    )
    assert paused["state"]["cell"]["status"] == "paused"
    exported = await scenario.store.export_company(COMPANY_ID)
    allocator_events = [
        event
        for event in exported["domain_events"]
        if event.get("actor_id") == PRINCIPAL_IDS["allocator"]
    ]
    assert allocator_events == []


def test_immune_is_not_a_signed_standing_agent() -> None:
    leftover = json.loads((AGENTS_ROOT / "immune" / "agent-definition.json").read_text())
    assert leftover["compatible_roles"] == ["immune"]
    assert leftover["name"] == "phase1-immune-monitor"
    assert Path(AGENTS_ROOT / "keeper").exists() is False
    assert Path(AGENTS_ROOT / "strategy").exists() is False

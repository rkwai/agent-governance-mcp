from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from pydantic import ValidationError as PydanticValidationError

from ai_nature_company.models import (
    AgentDefinition,
    Allocation,
    Constitution,
    Evaluation,
    EvidenceEnvelope,
    Experiment,
    Offer,
    WorkItem,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas"
AGENT_DIR = ROOT / "agents"
SCHEMA_NAMES = {
    "agent-definition-v1.json",
    "allocation-v1.json",
    "company-constitution-v1.json",
    "evaluation-v1.json",
    "evidence-v1.json",
    "experiment-v1.json",
    "offer-v1.json",
    "work-item-v1.json",
}

UUIDS = {
    "allocation": "00000000-0000-4000-8000-000000000001",
    "agent_run": "00000000-0000-4000-8000-000000000002",
    "cell": "00000000-0000-4000-8000-000000000003",
    "company": "00000000-0000-4000-8000-000000000004",
    "constitution": "00000000-0000-4000-8000-000000000005",
    "evaluator": "00000000-0000-4000-8000-000000000006",
    "evidence": "00000000-0000-4000-8000-000000000007",
    "executor": "00000000-0000-4000-8000-000000000008",
    "experiment": "00000000-0000-4000-8000-000000000009",
    "founder": "00000000-0000-4000-8000-000000000010",
    "offer": "00000000-0000-4000-8000-000000000011",
    "offer_version": "00000000-0000-4000-8000-000000000012",
    "principal": "00000000-0000-4000-8000-000000000013",
    "resource": "00000000-0000-4000-8000-000000000014",
    "work": "00000000-0000-4000-8000-000000000015",
}
NOW = "2026-08-29T00:00:00Z"
CONTRACT_MODELS = {
    "agent-definition-v1.json": AgentDefinition,
    "allocation-v1.json": Allocation,
    "company-constitution-v1.json": Constitution,
    "evaluation-v1.json": Evaluation,
    "evidence-v1.json": EvidenceEnvelope,
    "experiment-v1.json": Experiment,
    "offer-v1.json": Offer,
    "work-item-v1.json": WorkItem,
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        load_json(SCHEMA_DIR / name),
        format_checker=FormatChecker(),
    )


def valid_examples() -> dict[str, dict[str, Any]]:
    gates = [
        "legality_and_policy",
        "truthfulness_and_claim_support",
        "customer_outcome_floor",
        "security_and_privacy",
        "budget_and_cash",
        "delivery_quality_floor",
        "data_sufficiency_and_independence",
    ]
    return {
        "company-constitution-v1.json": {
            "schema_version": 1,
            "constitution_id": UUIDS["constitution"],
            "company_id": UUIDS["company"],
            "version": 1,
            "status": "active",
            "mission": "Create customer value and risk-adjusted contribution profit.",
            "authority_policy": {
                "founder_principal_ids": [UUIDS["founder"]],
                "irreversible_actions": ["company.dissolve"],
                "human_approval_actions": ["constitution.activate"],
            },
            "risk_policy": {
                "prohibited_actions": ["claim.fabricate"],
                "pricing_floor_minor": 10000,
                "max_external_messages_per_experiment": 10,
                "regulated_market_entry_requires_founder": True,
            },
            "budget_policy": {
                "currency": "USD",
                "company_limit_minor": 100000,
                "cell_limit_minor": 50000,
                "experiment_limit_minor": 10000,
                "single_action_limit_minor": 2500,
                "exploration_share_bps": 2000,
            },
            "fitness_policy": {
                "rule_version": "fitness-v1",
                "required_gates": gates,
                "minimum_evidence_count": 1,
                "minimum_customer_outcome_bps": 7000,
                "minimum_delivery_quality_bps": 8000,
                "allocation_cadence_days": 7,
            },
            "created_at": NOW,
            "created_by_principal_id": UUIDS["founder"],
            "effective_at": NOW,
            "supersedes_constitution_id": None,
        },
        "agent-definition-v1.json": load_json(AGENT_DIR / "specialist" / "agent-definition.json"),
        "offer-v1.json": {
            "schema_version": 1,
            "offer_version_id": UUIDS["offer_version"],
            "offer_id": UUIDS["offer"],
            "company_id": UUIDS["company"],
            "cell_id": UUIDS["cell"],
            "version": 1,
            "parent_offer_version_id": None,
            "target_segment": "Owner-led service firms",
            "urgent_problem": "Bespoke delivery constrains margin and sales speed.",
            "promised_outcome": "Package one repeatable workflow in four weeks.",
            "proof": ["Prior delivery-cycle reduction was independently verified."],
            "price": {"currency": "USD", "amount_minor": 500000},
            "commercial_model": "fixed_fee",
            "acquisition_channel": "Founder-led referral",
            "delivery_recipe": ["Map workflow", "Run bounded pilot", "Verify outcome"],
            "time_to_value_days": 28,
            "risk_reversal": "Stop after the unpaid sample if evidence does not hold.",
            "registered_at": NOW,
            "registered_by_principal_id": UUIDS["principal"],
        },
        "experiment-v1.json": {
            "schema_version": 1,
            "experiment_id": UUIDS["experiment"],
            "company_id": UUIDS["company"],
            "cell_id": UUIDS["cell"],
            "offer_version_id": UUIDS["offer_version"],
            "revision": 1,
            "state": "registered",
            "hypothesis": "A verified sample will produce one paid-pilot request.",
            "baseline": "No paid-pilot requests from zero samples.",
            "treatment": "Deliver one bounded sample to ten eligible firms.",
            "target_population": "Ten owner-led service firms matching the frozen ICP.",
            "metrics": [
                {
                    "metric_id": "paid_pilot_requests",
                    "name": "Paid pilot requests",
                    "unit": "count",
                    "direction": "increase",
                }
            ],
            "success_thresholds": [
                {"metric_id": "paid_pilot_requests", "operator": "gte", "value": 1}
            ],
            "failure_thresholds": [
                {"metric_id": "paid_pilot_requests", "operator": "eq", "value": 0}
            ],
            "stop_rules": [
                {
                    "rule_id": "budget_exhausted",
                    "condition": "Actual cost reaches the fixed experiment budget.",
                    "effect": "pause_external_actions",
                }
            ],
            "window": {"starts_at": NOW, "ends_at": "2026-09-05T00:00:00Z"},
            "budget": {"currency": "USD", "amount_minor": 10000},
            "permitted_actions": ["evidence.append"],
            "executor_principal_ids": [UUIDS["executor"]],
            "evaluator_principal_id": UUIDS["evaluator"],
            "registered_by_principal_id": UUIDS["principal"],
            "registered_at": NOW,
        },
        "evidence-v1.json": {
            "schema_version": 1,
            "evidence_id": UUIDS["evidence"],
            "company_id": UUIDS["company"],
            "cell_id": UUIDS["cell"],
            "offer_version_id": UUIDS["offer_version"],
            "experiment_id": UUIDS["experiment"],
            "type": "commitment",
            "source_system": "fake_crm",
            "source_reference": "fake_crm:commitment:1",
            "observed_at": NOW,
            "recorded_at": NOW,
            "strength": 1,
            "confidence_bps": 9000,
            "provenance_quality": "verified",
            "causal_distance": "direct",
            "half_life_seconds": 604800,
            "observation": {"value": 1, "unit": "count", "summary": "Paid pilot requested."},
            "lineage": {"relation": "original", "related_evidence_id": None},
            "recorded_by_principal_id": UUIDS["executor"],
            "agent_run_id": UUIDS["agent_run"],
            "external_action_id": None,
        },
        "work-item-v1.json": {
            "schema_version": 1,
            "work_item_id": UUIDS["work"],
            "company_id": UUIDS["company"],
            "cell_id": UUIDS["cell"],
            "experiment_id": UUIDS["experiment"],
            "revision": 1,
            "status": "open",
            "kind": "collect_verified_sample",
            "outcome_contract": {
                "outcome": "Record one attributable customer response.",
                "acceptance_criteria": ["Evidence cites the fake CRM receipt."],
                "result_schema_ref": "schemas/evidence-v1.json",
            },
            "eligible_capabilities": ["evidence.append"],
            "budget": {"currency": "USD", "maximum_cost_minor": 1000},
            "approval_requirements": ["none"],
            "input_references": [
                {"kind": "offer_version", "id": UUIDS["offer_version"], "revision": 1}
            ],
            "max_attempts": 3,
            "created_by_principal_id": UUIDS["principal"],
            "created_at": NOW,
            "available_at": NOW,
            "deadline_at": "2026-09-05T00:00:00Z",
        },
        "evaluation-v1.json": {
            "schema_version": 1,
            "evaluation_id": UUIDS["resource"],
            "company_id": UUIDS["company"],
            "experiment_id": UUIDS["experiment"],
            "experiment_revision": 3,
            "evaluator_principal_id": UUIDS["evaluator"],
            "evaluator_run_id": UUIDS["agent_run"],
            "executor_principal_ids": [UUIDS["executor"]],
            "independence_attestation": {
                "is_independent": True,
                "checked_by_substrate": True,
                "basis": "separate_principal",
            },
            "criterion_results": [
                {
                    "criterion_id": "paid_pilot_requests",
                    "outcome": "passed",
                    "observed_value": 1,
                    "evidence_ids": [UUIDS["evidence"]],
                }
            ],
            "result": "passed",
            "summary": "The frozen success threshold was met.",
            "evaluated_at": NOW,
        },
        "allocation-v1.json": {
            "schema_version": 1,
            "allocation_id": UUIDS["allocation"],
            "company_id": UUIDS["company"],
            "revision": 1,
            "decision_state": "proposed",
            "action": "fund",
            "target": {"kind": "cell", "id": UUIDS["cell"], "revision": 4},
            "stage": "exploitation",
            "gate_results": [
                {"gate": gate, "passed": True, "evidence_ids": [UUIDS["evidence"]]}
                for gate in gates
            ],
            "ranking": {
                "formula": "exploitation-v1",
                "score_numerator": 810000000000000,
                "score_denominator": 30000000000000,
                "input_snapshot": {
                    "expected_12m_contribution_profit_minor": 500000,
                    "evidence_confidence_bps": 9000,
                    "expected_repeatability_bps": 6000,
                    "capital_at_risk_minor": 10000,
                    "time_to_cash_days": 30,
                },
            },
            "amount": {"currency": "USD", "amount_minor": 10000},
            "constitution_id": UUIDS["constitution"],
            "constitution_version": 1,
            "rule_version": "fitness-v1",
            "input_digest": "a" * 64,
            "rationale_summary": (
                "All gates pass and this is the highest eligible exploitation rank."
            ),
            "proposed_by_principal_id": UUIDS["principal"],
            "proposed_by_run_id": UUIDS["agent_run"],
            "proposed_at": NOW,
            "decided_by_principal_id": None,
            "decided_by_run_id": None,
            "decided_at": None,
        },
    }


def test_expected_schema_set_is_present() -> None:
    assert {path.name for path in SCHEMA_DIR.glob("*.json")} == SCHEMA_NAMES


@pytest.mark.parametrize("schema_name", sorted(SCHEMA_NAMES))
def test_schema_is_valid_draft_2020_12(schema_name: str) -> None:
    schema = load_json(SCHEMA_DIR / schema_name)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith(f"/{schema_name}")
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    Draft202012Validator.check_schema(schema)

    external_refs = [
        value
        for value in _values_for_key(schema, "$ref")
        if isinstance(value, str) and not value.startswith("#/")
    ]
    assert external_refs == []


@pytest.mark.parametrize(("schema_name", "instance"), valid_examples().items())
def test_valid_v1_examples(schema_name: str, instance: dict[str, Any]) -> None:
    validator(schema_name).validate(instance)


@pytest.mark.parametrize(("schema_name", "instance"), valid_examples().items())
def test_valid_v1_examples_match_runtime_contract_models(
    schema_name: str, instance: dict[str, Any]
) -> None:
    validated = CONTRACT_MODELS[schema_name].model_validate(instance)

    assert validated.schema_version == 1
    validator(schema_name).validate(validated.model_dump(mode="json"))


@pytest.mark.parametrize(("schema_name", "instance"), valid_examples().items())
def test_schemas_reject_unknown_top_level_fields(
    schema_name: str, instance: dict[str, Any]
) -> None:
    invalid = copy.deepcopy(instance)
    invalid["caller_role"] = "founder"

    with pytest.raises(ValidationError):
        validator(schema_name).validate(invalid)


@pytest.mark.parametrize(("schema_name", "instance"), valid_examples().items())
def test_schemas_reject_missing_schema_version(schema_name: str, instance: dict[str, Any]) -> None:
    invalid = copy.deepcopy(instance)
    del invalid["schema_version"]

    with pytest.raises(ValidationError):
        validator(schema_name).validate(invalid)


def test_offer_lineage_requires_mutation_summary_only_for_children() -> None:
    offer = valid_examples()["offer-v1.json"]
    child = copy.deepcopy(offer)
    child["offer_version_id"] = UUIDS["resource"]
    child["version"] = 2
    child["parent_offer_version_id"] = UUIDS["offer_version"]

    with pytest.raises(ValidationError):
        validator("offer-v1.json").validate(child)

    child["mutation_summary"] = "Narrowed the target segment."
    validator("offer-v1.json").validate(child)

    root_with_mutation = copy.deepcopy(offer)
    root_with_mutation["mutation_summary"] = "No parent exists."
    with pytest.raises(ValidationError):
        validator("offer-v1.json").validate(root_with_mutation)


def test_evidence_correction_requires_a_lineage_target() -> None:
    evidence = valid_examples()["evidence-v1.json"]
    correction = copy.deepcopy(evidence)
    correction["lineage"] = {"relation": "correction", "related_evidence_id": None}

    with pytest.raises(ValidationError):
        validator("evidence-v1.json").validate(correction)

    correction["lineage"]["related_evidence_id"] = UUIDS["resource"]
    validator("evidence-v1.json").validate(correction)


def test_evaluation_contract_requires_server_checked_independence() -> None:
    evaluation = valid_examples()["evaluation-v1.json"]
    evaluation["independence_attestation"]["checked_by_substrate"] = False

    with pytest.raises(ValidationError):
        validator("evaluation-v1.json").validate(evaluation)


def test_experiment_preregisters_a_distinct_evaluator() -> None:
    experiment = valid_examples()["experiment-v1.json"]

    assert experiment["evaluator_principal_id"] not in experiment["executor_principal_ids"]

    invalid = copy.deepcopy(experiment)
    invalid["evaluator_principal_id"] = invalid["executor_principal_ids"][0]
    with pytest.raises(PydanticValidationError, match="cannot be an experiment executor"):
        Experiment.model_validate(invalid)


def test_inconclusive_criterion_may_record_no_supporting_evidence() -> None:
    evaluation = valid_examples()["evaluation-v1.json"]
    criterion = evaluation["criterion_results"][0]
    criterion["outcome"] = "inconclusive"
    criterion["observed_value"] = None
    criterion["evidence_ids"] = []
    evaluation["result"] = "inconclusive"
    evaluation["summary"] = "No attributable observation arrived inside the window."

    validator("evaluation-v1.json").validate(evaluation)

    criterion["outcome"] = "passed"
    evaluation["result"] = "passed"
    with pytest.raises(ValidationError):
        validator("evaluation-v1.json").validate(evaluation)


def test_failed_count_criterion_may_record_zero_without_evidence() -> None:
    evaluation = valid_examples()["evaluation-v1.json"]
    criterion = evaluation["criterion_results"][0]
    criterion["outcome"] = "failed"
    criterion["observed_value"] = 0
    criterion["evidence_ids"] = []
    evaluation["result"] = "failed"
    evaluation["summary"] = "Nothing happened; the count criterion failed."

    validator("evaluation-v1.json").validate(evaluation)

    criterion["observed_value"] = 1
    with pytest.raises(ValidationError):
        validator("evaluation-v1.json").validate(evaluation)


def test_committed_allocation_requires_decision_attribution() -> None:
    allocation = valid_examples()["allocation-v1.json"]
    allocation["decision_state"] = "committed"

    with pytest.raises(ValidationError):
        validator("allocation-v1.json").validate(allocation)

    allocation["decided_by_principal_id"] = UUIDS["evaluator"]
    allocation["decided_at"] = NOW
    # A server-verified human founder can commit an irreversible decision
    # without inventing an agent run. Principal and server time remain required.
    validator("allocation-v1.json").validate(allocation)

    allocation["decided_by_run_id"] = UUIDS["resource"]
    validator("allocation-v1.json").validate(allocation)


def test_allocation_requires_each_gate_exactly_once() -> None:
    allocation = valid_examples()["allocation-v1.json"]
    allocation["gate_results"][-1] = {
        "gate": "budget_and_cash",
        "passed": True,
        "evidence_ids": [],
    }

    with pytest.raises(ValidationError):
        validator("allocation-v1.json").validate(allocation)


def test_allocation_stage_selects_the_matching_formula_inputs() -> None:
    allocation = valid_examples()["allocation-v1.json"]
    allocation["ranking"]["formula"] = "exploration-v1"

    with pytest.raises(ValidationError):
        validator("allocation-v1.json").validate(allocation)


def test_non_funding_allocation_cannot_carry_an_amount() -> None:
    allocation = valid_examples()["allocation-v1.json"]
    allocation["action"] = "maintain"

    with pytest.raises(ValidationError):
        validator("allocation-v1.json").validate(allocation)

    allocation["amount"] = None
    validator("allocation-v1.json").validate(allocation)


def test_exploitation_score_fraction_matches_fitness_v1() -> None:
    ranking = valid_examples()["allocation-v1.json"]["ranking"]
    inputs = ranking["input_snapshot"]
    expected_numerator = (
        inputs["expected_12m_contribution_profit_minor"]
        * inputs["evidence_confidence_bps"]
        * inputs["expected_repeatability_bps"]
        * 30
    )
    expected_denominator = (
        max(inputs["capital_at_risk_minor"], 1)
        * max(inputs["time_to_cash_days"], 30)
        * 10000
        * 10000
    )

    assert ranking["score_numerator"] == expected_numerator
    assert ranking["score_denominator"] == expected_denominator

    invalid = copy.deepcopy(valid_examples()["allocation-v1.json"])
    invalid["ranking"]["score_numerator"] += 1
    with pytest.raises(PydanticValidationError, match="does not match fitness-v1"):
        Allocation.model_validate(invalid)


def test_budget_units_accept_time_tries_and_cycles_and_reject_mixes() -> None:
    constitution = valid_examples()["company-constitution-v1.json"]
    experiment = valid_examples()["experiment-v1.json"]
    work_item = valid_examples()["work-item-v1.json"]
    allocation = valid_examples()["allocation-v1.json"]

    time_policy = {
        **constitution["budget_policy"],
        "unit": "time",
    }
    del time_policy["currency"]
    constitution["budget_policy"] = time_policy
    validator("company-constitution-v1.json").validate(constitution)
    Constitution.model_validate(constitution)

    experiment["budget"] = {"unit": "tries", "amount_minor": 4}
    validator("experiment-v1.json").validate(experiment)
    work_item["budget"] = {"unit": "cycles", "maximum_cost_minor": 12}
    validator("work-item-v1.json").validate(work_item)
    allocation["amount"] = {"unit": "time", "amount_minor": 90}
    validator("allocation-v1.json").validate(allocation)

    mixed = valid_examples()["experiment-v1.json"]
    mixed["budget"] = {"unit": "tries", "currency": "USD", "amount_minor": 4}
    with pytest.raises(ValidationError):
        validator("experiment-v1.json").validate(mixed)
    with pytest.raises(PydanticValidationError):
        Experiment.model_validate(mixed)


def test_work_item_none_approval_cannot_mix_with_founder_approval() -> None:
    work_item = valid_examples()["work-item-v1.json"]
    work_item["approval_requirements"] = ["none", "founder_before_claim"]

    with pytest.raises(ValidationError):
        validator("work-item-v1.json").validate(work_item)


def test_work_item_schema_accepts_null_claim_observations() -> None:
    work_item = valid_examples()["work-item-v1.json"]
    work_item.update(
        {
            "claimant_agent_id": None,
            "lease_id": None,
            "lease_expires_at": None,
            "last_heartbeat_at": None,
        }
    )
    validator("work-item-v1.json").validate(work_item)

    claimed = copy.deepcopy(valid_examples()["work-item-v1.json"])
    claimed.update(
        {
            "status": "claimed",
            "claimant_agent_id": UUIDS["principal"],
            "lease_id": UUIDS["work"],
            "lease_expires_at": "2026-08-29T01:00:00Z",
            "last_heartbeat_at": NOW,
        }
    )
    validator("work-item-v1.json").validate(claimed)
    observed = WorkItem.model_validate(claimed)
    assert observed.claimant_agent_id is not None
    assert observed.lease_id is not None
    assert observed.last_heartbeat_at is not None


def _values_for_key(value: Any, key: str) -> list[Any]:
    if isinstance(value, dict):
        found = [value[key]] if key in value else []
        for child in value.values():
            found.extend(_values_for_key(child, key))
        return found
    if isinstance(value, list):
        found = []
        for child in value:
            found.extend(_values_for_key(child, key))
        return found
    return []

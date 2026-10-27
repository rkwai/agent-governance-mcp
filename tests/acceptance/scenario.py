"""Reusable black-box Phase 1 vertical-slice driver for acceptance tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ai_nature_company.service import SubstrateService, create_service
from ai_nature_company.store import InMemoryStore

from .support import (
    ALLOCATION_ID,
    CELL_BUDGET_ID,
    CELL_ID,
    COMPANY_BUDGET_ID,
    COMPANY_ID,
    CONSTITUTION_ID,
    EVALUATION_ID,
    EVIDENCE_ID,
    EXPERIMENT_ID,
    MUTATED_OFFER_VERSION_ID,
    OFFER_ID,
    OFFER_VERSION_ID,
    PRINCIPAL_IDS,
    WORK_ITEM_ID,
    MutableClock,
    load_definition,
    make_principals,
    mutation,
    stable_uuid,
)

REQUIRED_GATES = (
    "legality_and_policy",
    "truthfulness_and_claim_support",
    "customer_outcome_floor",
    "security_and_privacy",
    "budget_and_cash",
    "delivery_quality_floor",
    "data_sufficiency_and_independence",
)


class Scenario:
    """One isolated company operated only through the public service boundary."""

    def __init__(self) -> None:
        self.clock = MutableClock()
        self.store = InMemoryStore()
        self.service: SubstrateService = create_service(store=self.store, clock=self.clock)
        self.principals = make_principals()
        self.responses: dict[str, dict[str, Any]] = {}

    async def invoke(
        self,
        tool_name: str,
        actor: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        key: str | None = None,
        correlation: str = "vertical-slice",
    ) -> dict[str, Any]:
        response = await self.service.invoke(
            tool_name,
            self.principals[actor],
            mutation(
                tool_name,
                payload,
                expected_revision=expected_revision,
                key=key,
                correlation=correlation,
            ),
        )
        materialized = dict(response)
        assert set(materialized) == {
            "operation_id",
            "prior_revision",
            "new_revision",
            "event_ids",
            "state",
            "audit_id",
            "replayed",
        }
        assert materialized["prior_revision"] == expected_revision
        assert materialized["new_revision"] == expected_revision + 1
        assert materialized["event_ids"]
        assert materialized["audit_id"]
        assert isinstance(materialized["state"], dict)
        assert isinstance(materialized["replayed"], bool)
        self.responses[f"{tool_name}:{key or expected_revision}"] = materialized
        return materialized

    @staticmethod
    def _agent_payloads() -> list[dict[str, Any]]:
        actors = {
            "cell-owner": "owner",
            "specialist": "specialist",
            "evaluator": "evaluator",
            "allocator": "allocator",
            "immune": "immune",
        }
        payloads: list[dict[str, Any]] = []
        for kind, actor in actors.items():
            definition = load_definition(kind)
            payloads.append(
                {
                    "principal_id": PRINCIPAL_IDS[actor],
                    "cell_id": CELL_ID if actor in {"owner", "specialist", "evaluator"} else None,
                    "definition": definition,
                }
            )
        return payloads

    async def bootstrap(
        self,
        *,
        constitution_currency: str | None = "USD",
        constitution_unit: str = "money",
        company_limit_minor: int = 100_000,
        cell_limit_minor: int = 50_000,
        experiment_limit_minor: int = 10_000,
        single_action_limit_minor: int = 10_000,
        budget_currency: str | None = "USD",
        budget_unit: str = "money",
        budget_limit: str | int = "1000.00",
        key: str = "bootstrap-company-v1",
    ) -> dict[str, Any]:
        budget_policy: dict[str, Any] = {
            "unit": constitution_unit,
            "company_limit_minor": company_limit_minor,
            "cell_limit_minor": cell_limit_minor,
            "experiment_limit_minor": experiment_limit_minor,
            "single_action_limit_minor": single_action_limit_minor,
            "exploration_share_bps": 2_000,
        }
        if constitution_unit == "money":
            budget_policy["currency"] = constitution_currency
        company_budget: dict[str, Any] = {
            "budget_id": COMPANY_BUDGET_ID,
            "limit": budget_limit,
            "duration_days": 365,
        }
        if budget_unit == "money":
            company_budget["currency"] = budget_currency
        else:
            company_budget["unit"] = budget_unit
        response = await self.invoke(
            "company.bootstrap",
            "founder",
            {
                "company_id": COMPANY_ID,
                "name": "AI Nature Company Acceptance",
                "constitution": {
                    "schema_version": 1,
                    "constitution_id": CONSTITUTION_ID,
                    "version": 1,
                    "mission": (
                        "Create verified customer value while maximizing "
                        "risk-adjusted contribution profit."
                    ),
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
                    "budget_policy": budget_policy,
                    "fitness_policy": {
                        "rule_version": "fitness-v1",
                        "required_gates": list(REQUIRED_GATES),
                        "minimum_evidence_count": 1,
                        "minimum_customer_outcome_bps": 7_000,
                        "minimum_delivery_quality_bps": 8_000,
                        "allocation_cadence_days": 7,
                    },
                },
                "agents": self._agent_payloads(),
                "budget": company_budget,
            },
            expected_revision=0,
            key=key,
        )
        self.responses["bootstrap"] = response
        return response

    async def create_cell(
        self,
        *,
        budget_limit: str | int = "500.00",
        currency: str | None = "USD",
        unit: str | None = None,
        key: str = "create-cell-v1",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cell_id": CELL_ID,
            "name": "Productized AI workflow",
            "objective": "Find a repeatable paid offer with verified outcomes.",
            "budget_id": CELL_BUDGET_ID,
            "budget_limit": budget_limit,
            "budget_duration_days": 90,
        }
        if unit is not None and unit != "money":
            payload["unit"] = unit
        elif currency is not None:
            payload["currency"] = currency
        response = await self.invoke(
            "cell.create",
            "owner",
            payload,
            expected_revision=0,
            key=key,
        )
        self.responses["cell.create"] = response
        return response

    async def register_offer(self) -> dict[str, Any]:
        response = await self.invoke(
            "offer.register",
            "owner",
            {
                "offer_version_id": OFFER_VERSION_ID,
                "offer_id": OFFER_ID,
                "cell_id": CELL_ID,
                "target_segment": "Owner-led service firms",
                "urgent_problem": "Bespoke delivery constrains margin and sales speed.",
                "promised_outcome": "Package one repeatable workflow in four weeks.",
                "proof": ["A prior delivery-cycle reduction was independently verified."],
                "price": {"currency": "USD", "amount_minor": 500_000},
                "commercial_model": "fixed_fee",
                "acquisition_channel": "Founder-led referral",
                "delivery_recipe": [
                    "Map one workflow",
                    "Run one bounded pilot",
                    "Verify the customer outcome",
                ],
                "time_to_value_days": 28,
                "risk_reversal": "Stop after the sample when evidence does not hold.",
            },
            expected_revision=self.cell_revision,
            key="register-offer-v1",
        )
        self.responses["offer.register"] = response
        return response

    async def mutate_offer(self) -> dict[str, Any]:
        response = await self.invoke(
            "offer.mutate",
            "owner",
            {
                "parent_offer_version_id": OFFER_VERSION_ID,
                "offer_version_id": MUTATED_OFFER_VERSION_ID,
                "mutation_summary": "Narrowed the segment and raised proof-backed price.",
                "changes": {
                    "target_segment": "Owner-led data and engineering service firms",
                    "price": {"currency": "USD", "amount_minor": 600_000},
                },
            },
            expected_revision=self.offer_revision,
            key="mutate-offer-v2",
        )
        self.responses["offer.mutate"] = response
        return response

    async def activate_cell(self) -> None:
        ready = await self.invoke(
            "cell.transition",
            "owner",
            {"cell_id": CELL_ID, "target_status": "ready"},
            expected_revision=self.cell_revision,
            key="cell-ready",
        )
        self.responses["cell.ready"] = ready
        active = await self.invoke(
            "cell.transition",
            "owner",
            {"cell_id": CELL_ID, "target_status": "active"},
            expected_revision=self.cell_revision,
            key="cell-active",
        )
        self.responses["cell.active"] = active

    async def register_experiment(
        self,
        *,
        experiment_id: str = EXPERIMENT_ID,
        budget_minor: int = 10_000,
        budget: dict[str, Any] | None = None,
        executors: list[str] | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        starts_at = self.clock().isoformat().replace("+00:00", "Z")
        ends_at = (self.clock() + timedelta(days=7)).isoformat().replace("+00:00", "Z")
        response = await self.invoke(
            "experiment.register",
            "owner",
            {
                "experiment_id": experiment_id,
                "cell_id": CELL_ID,
                "offer_version_id": MUTATED_OFFER_VERSION_ID,
                "hypothesis": "Ten verified samples will produce one paid-pilot request.",
                "baseline": "Zero paid-pilot requests from zero verified samples.",
                "treatment": "Deliver a bounded sample to ten eligible firms.",
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
                    },
                    {
                        "rule_id": "policy_violation",
                        "condition": "Verified evidence records a policy violation.",
                        "effect": "cancel_experiment",
                    },
                ],
                "window": {"starts_at": starts_at, "ends_at": ends_at},
                "budget": budget or {"currency": "USD", "amount_minor": budget_minor},
                "permitted_actions": ["evidence.append"],
                "executor_principal_ids": executors or [PRINCIPAL_IDS["specialist"]],
                "evaluator_principal_id": PRINCIPAL_IDS["evaluator"],
            },
            expected_revision=0,
            key=key or f"register-experiment:{experiment_id}",
        )
        self.responses[f"experiment.register:{experiment_id}"] = response
        return response

    async def start_experiment(
        self,
        *,
        experiment_id: str = EXPERIMENT_ID,
        expected_revision: int | None = None,
        key: str = "start-experiment",
    ) -> dict[str, Any]:
        response = await self.invoke(
            "experiment.start",
            "owner",
            {"experiment_id": experiment_id},
            expected_revision=(
                self.experiment_revision(experiment_id)
                if expected_revision is None
                else expected_revision
            ),
            key=key,
        )
        self.responses[f"experiment.start:{experiment_id}"] = response
        return response

    async def create_work(
        self,
        *,
        cell_id: str | None = CELL_ID,
        experiment_id: str | None = EXPERIMENT_ID,
        work_item_id: str = WORK_ITEM_ID,
        key: str = "create-work",
        max_attempts: int = 3,
        kind: str = "collect_verified_sample",
        budget: dict[str, Any] | None = None,
        deadline_at: str | None = None,
    ) -> dict[str, Any]:
        deadline = deadline_at or (self.clock() + timedelta(days=7)).isoformat().replace(
            "+00:00", "Z"
        )
        response = await self.invoke(
            "work.create",
            "owner",
            {
                "work_item_id": work_item_id,
                "cell_id": cell_id,
                "experiment_id": experiment_id,
                "kind": kind,
                "outcome_contract": {
                    "outcome": "Record one attributable customer response.",
                    "acceptance_criteria": ["Evidence cites the fake CRM receipt."],
                    "result_schema_ref": "schemas/evidence-v1.json",
                },
                "eligible_capabilities": ["evidence.append"],
                "budget": budget or {"currency": "USD", "maximum_cost_minor": 1_000},
                "approval_requirements": ["none"],
                "input_references": [
                    {
                        "kind": "offer_version",
                        "id": MUTATED_OFFER_VERSION_ID,
                        "revision": 2,
                    }
                ],
                "max_attempts": max_attempts,
                "deadline_at": deadline,
            },
            expected_revision=0,
            key=key,
        )
        self.responses["work.create"] = response
        self.responses[f"work.create:{work_item_id}"] = response
        return response

    async def claim_work(
        self,
        *,
        work_item_id: str = WORK_ITEM_ID,
        expected_revision: int | None = None,
        key: str = "claim-work",
        actor: str = "specialist",
        lease_seconds: int = 30,
    ) -> dict[str, Any]:
        response = await self.invoke(
            "work.claim",
            actor,
            {"work_item_id": work_item_id, "lease_seconds": lease_seconds},
            expected_revision=(
                self.work_item_revision(work_item_id)
                if expected_revision is None
                else expected_revision
            ),
            key=key,
        )
        self.responses["work.claim"] = response
        self.responses[f"work.claim:{work_item_id}"] = response
        return response

    async def append_evidence(
        self,
        *,
        evidence_id: str = EVIDENCE_ID,
        evidence_type: str = "outcome",
        experiment_id: str | None = EXPERIMENT_ID,
        actor: str = "specialist",
        key: str = "append-evidence",
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self.invoke(
            "evidence.append",
            actor,
            {
                "evidence_id": evidence_id,
                "company_id": stable_uuid("spoofed-company"),
                "cell_id": CELL_ID,
                "offer_version_id": MUTATED_OFFER_VERSION_ID,
                "experiment_id": experiment_id,
                "type": evidence_type,
                "source_system": "fake_crm",
                "source_reference": f"fake_crm:{evidence_type}:1",
                "observed_at": self.clock().isoformat().replace("+00:00", "Z"),
                "strength": 1,
                "confidence_bps": 9_000,
                "provenance_quality": "verified",
                "causal_distance": "direct",
                "half_life_seconds": 604_800,
                "observation": observation
                or {
                    "value": 9_000 if evidence_type == "outcome" else 1,
                    "unit": "basis_points" if evidence_type == "outcome" else "count",
                    "summary": "A verified customer outcome met the frozen threshold.",
                },
                "lineage": {"relation": "original", "related_evidence_id": None},
                "recorded_by_principal_id": stable_uuid("spoofed-principal"),
                "agent_run_id": stable_uuid("spoofed-run"),
                "external_action_id": None,
            },
            expected_revision=0,
            key=key,
        )
        self.responses[f"evidence.append:{evidence_id}"] = response
        return response

    async def complete_work(
        self,
        *,
        work_item_id: str = WORK_ITEM_ID,
        evidence_id: str = EVIDENCE_ID,
        key: str = "complete-work",
        actual_cost: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lease = self.responses.get(f"work.claim:{work_item_id}", self.responses["work.claim"])[
            "state"
        ]["lease"]
        response = await self.invoke(
            "work.complete",
            "specialist",
            {
                "work_item_id": work_item_id,
                "lease_token": lease["token"],
                "result_evidence_id": evidence_id,
                "actual_cost": actual_cost or {"currency": "USD", "amount_minor": 500},
            },
            expected_revision=self.work_item_revision(work_item_id),
            key=key,
        )
        self.responses["work.complete"] = response
        self.responses[f"work.complete:{work_item_id}"] = response
        return response

    async def fail_work(
        self,
        *,
        work_item_id: str = WORK_ITEM_ID,
        failure_code: str = "hunt_missed",
        retryable: bool = False,
        actual_cost: dict[str, Any] | None = None,
        key: str = "fail-work",
    ) -> dict[str, Any]:
        lease = self.responses.get(f"work.claim:{work_item_id}", self.responses["work.claim"])[
            "state"
        ]["lease"]
        payload: dict[str, Any] = {
            "work_item_id": work_item_id,
            "lease_token": lease["token"],
            "failure_code": failure_code,
            "retryable": retryable,
        }
        if actual_cost is not None:
            payload["actual_cost"] = actual_cost
        response = await self.invoke(
            "work.fail",
            "specialist",
            payload,
            expected_revision=self.work_item_revision(work_item_id),
            key=key,
        )
        self.responses["work.fail"] = response
        self.responses[f"work.fail:{work_item_id}"] = response
        return response

    async def close_experiment(self) -> dict[str, Any]:
        return await self.close_named_experiment(EXPERIMENT_ID)

    async def close_named_experiment(
        self,
        experiment_id: str,
        *,
        actual_cost: dict[str, Any] | None = None,
        reason: str | None = None,
        founder_decision_ref: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "experiment_id": experiment_id,
            "actual_cost": actual_cost or {"currency": "USD", "amount_minor": 500},
        }
        if reason is not None:
            payload["reason"] = reason
        if founder_decision_ref is not None:
            payload["founder_decision_ref"] = founder_decision_ref
        response = await self.invoke(
            "experiment.close",
            "owner",
            payload,
            expected_revision=self.experiment_revision(experiment_id),
            key=key or f"close-experiment:{experiment_id}",
        )
        self.responses[f"experiment.close:{experiment_id}"] = response
        if experiment_id == EXPERIMENT_ID:
            self.responses["experiment.close"] = response
        return response

    async def submit_evaluation(
        self,
        *,
        actor: str = "evaluator",
        experiment_id: str = EXPERIMENT_ID,
        evidence_id: str = EVIDENCE_ID,
        expected_revision: int | None = None,
        result: str = "passed",
        window_ends_at: str | None = None,
        key: str = "submit-evaluation",
    ) -> dict[str, Any]:
        if window_ends_at is None:
            async with self.store.transaction(COMPANY_ID) as tx:
                record = await tx.get_projection("experiment", experiment_id)
                assert record is not None
                window_ends_at = str(record["state"]["window"]["ends_at"])
        response = await self.invoke(
            "evaluation.submit",
            actor,
            {
                "evaluation_id": EVALUATION_ID,
                "experiment_id": experiment_id,
                "experiment_revision": (
                    self.experiment_revision(experiment_id)
                    if expected_revision is None
                    else expected_revision
                ),
                "evaluator_principal_id": stable_uuid("spoofed-evaluator"),
                "evaluator_run_id": stable_uuid("spoofed-evaluator-run"),
                "executor_principal_ids": [PRINCIPAL_IDS["specialist"]],
                "window_ends_at": window_ends_at,
                "criterion_results": [
                    {
                        "criterion_id": "paid_pilot_requests",
                        "outcome": result,
                        "observed_value": 1,
                        "evidence_ids": [evidence_id],
                    }
                ],
                "result": result,
                "summary": "The frozen success threshold was met with attributable evidence.",
            },
            expected_revision=(
                self.experiment_revision(experiment_id)
                if expected_revision is None
                else expected_revision
            ),
            key=key,
        )
        self.responses[f"evaluation.submit:{experiment_id}"] = response
        return response

    async def evaluate_gates(
        self,
        *,
        actor: str = "allocator",
        key: str = "evaluate-all-gates",
    ) -> dict[str, Any]:
        response = await self.invoke(
            "gate.evaluate",
            actor,
            {
                "action": "fund",
                "stage": "exploitation",
                "target": {
                    "kind": "cell",
                    "id": CELL_ID,
                    "revision": self.cell_revision,
                },
                "evaluation_id": EVALUATION_ID,
                "amount": {"currency": "USD", "amount_minor": 9_000},
            },
            expected_revision=0,
            key=key,
        )
        self.responses["gate.evaluate"] = response
        return response

    async def propose_allocation(
        self,
        *,
        allocation_id: str = ALLOCATION_ID,
        amount_minor: int = 9_000,
        key: str = "propose-allocation",
        actor: str = "allocator",
    ) -> dict[str, Any]:
        gate_state = self.responses["gate.evaluate"]["state"]
        decision = gate_state.get("decision", gate_state.get("gate_result", gate_state))
        response = await self.invoke(
            "allocation.propose",
            actor,
            {
                "allocation_id": allocation_id,
                "action": "fund",
                "target": {
                    "kind": "cell",
                    "id": CELL_ID,
                    "revision": self.cell_revision,
                },
                "stage": "exploitation",
                "gate_decision_id": decision.get("decision_id", decision.get("gate_result_id")),
                "ranking": {
                    "formula": "exploitation-v1",
                    "score_numerator": 1,
                    "score_denominator": 999,
                    "input_snapshot": {
                        "expected_12m_contribution_profit_minor": 500_000,
                        "evidence_confidence_bps": 9_000,
                        "expected_repeatability_bps": 6_000,
                        "capital_at_risk_minor": 10_000,
                        "time_to_cash_days": 30,
                    },
                },
                "amount": {"currency": "USD", "amount_minor": amount_minor},
                "rationale_summary": "All seven gates pass under the frozen fitness rule.",
                "proposed_by_principal_id": stable_uuid("spoofed-allocator"),
                "proposed_by_run_id": stable_uuid("spoofed-allocation-run"),
            },
            expected_revision=0,
            key=key,
        )
        self.responses["allocation.propose"] = response
        return response

    async def commit_allocation(
        self,
        *,
        actor: str = "allocator",
        key: str = "commit-allocation",
    ) -> dict[str, Any]:
        proposal = self.responses["allocation.propose"]["state"]["allocation"]
        response = await self.invoke(
            "allocation.commit",
            actor,
            {"allocation_id": ALLOCATION_ID},
            expected_revision=int(proposal.get("revision", 1)),
            key=key,
        )
        self.responses["allocation.commit"] = response
        return response

    @property
    def cell_revision(self) -> int:
        for key in (
            "cell.active",
            "cell.ready",
            "offer.mutate",
            "offer.register",
            "cell.create",
        ):
            response = self.responses.get(key)
            if response and "cell" in response["state"]:
                return int(response["state"]["cell"]["revision"])
        raise AssertionError("cell has not been created")

    @property
    def offer_revision(self) -> int:
        offer = self.responses["offer.register"]["state"]["offer"]
        return int(offer.get("revision", offer.get("version", 1) - 1))

    @property
    def work_revision(self) -> int:
        return self.work_item_revision(WORK_ITEM_ID)

    def work_item_revision(self, work_item_id: str = WORK_ITEM_ID) -> int:
        for key in (
            f"work.cancel:{work_item_id}",
            f"work.fail:{work_item_id}",
            f"work.complete:{work_item_id}",
            f"work.claim:{work_item_id}",
            f"work.create:{work_item_id}",
            "work.cancel",
            "work.fail",
            "work.complete",
            "work.claim",
            "work.create",
        ):
            response = self.responses.get(key)
            if response and "work_item" in response["state"]:
                work = response["state"]["work_item"]
                if str(work.get("work_item_id")) == work_item_id:
                    return int(work.get("revision", 0))
        raise AssertionError(f"work {work_item_id} has not been created")

    def experiment_revision(self, experiment_id: str = EXPERIMENT_ID) -> int:
        for key in (
            f"evaluation.submit:{experiment_id}",
            f"experiment.amend:{experiment_id}",
            f"experiment.close:{experiment_id}",
            f"experiment.start:{experiment_id}",
            f"experiment.register:{experiment_id}",
        ):
            response = self.responses.get(key)
            if response and "experiment" in response["state"]:
                return int(response["state"]["experiment"]["revision"])
        raise AssertionError(f"experiment {experiment_id} has not been registered")

    async def build_offer_cell(self) -> None:
        await self.bootstrap()
        await self.create_cell()
        await self.register_offer()
        await self.mutate_offer()
        await self.activate_cell()

    async def build_running_experiment(self) -> None:
        await self.build_offer_cell()
        await self.register_experiment()
        await self.start_experiment()

    async def build_evaluating_experiment(self) -> None:
        await self.build_running_experiment()
        await self.create_work()
        await self.claim_work()
        await self.append_evidence()
        await self.complete_work()
        await self.close_experiment()

    async def build_evaluated_experiment(self) -> None:
        await self.build_evaluating_experiment()
        await self.submit_evaluation()

    async def build_allocation(self) -> None:
        await self.build_evaluated_experiment()
        await self.evaluate_gates()
        await self.propose_allocation()
        await self.commit_allocation()

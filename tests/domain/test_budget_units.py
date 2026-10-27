"""Contract tests for money, time, cycles, and tries units of account."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from ai_nature_company.ledger_units import TIME_SCALE, dump_budget_amount, identity_of
from ai_nature_company.models import (
    AuthorityPolicy,
    BudgetAmount,
    BudgetPolicy,
    Constitution,
    ConstitutionStatus,
    FitnessPolicy,
    GateName,
    RiskPolicy,
    UnitOfAccount,
    WorkBudget,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE = Path("fixtures/closed_beta_two_cell_example.json")


def _policy(**overrides: object) -> BudgetPolicy:
    values: dict[str, object] = {
        "currency": "USD",
        "company_limit_minor": 100_000,
        "cell_limit_minor": 50_000,
        "experiment_limit_minor": 10_000,
        "single_action_limit_minor": 5_000,
        "exploration_share_bps": 2_000,
    }
    values.update(overrides)
    return BudgetPolicy.model_validate(values)


def test_each_unit_is_first_class_on_budget_policy_and_amounts() -> None:
    usd = _policy()
    assert usd.unit is UnitOfAccount.MONEY
    assert usd.currency == "USD"
    assert "unit" not in usd.model_dump(mode="json")

    seconds = _policy(
        unit="time",
        currency=None,
        company_limit_minor=36_000,
        cell_limit_minor=18_000,
        experiment_limit_minor=3_600,
        single_action_limit_minor=3_600,
    )
    assert seconds.unit is UnitOfAccount.TIME
    assert seconds.currency is None
    assert seconds.model_dump(mode="json")["unit"] == "time"
    assert "currency" not in seconds.model_dump(mode="json")

    cycles = BudgetAmount(unit="cycles", amount_minor=40)
    tries = WorkBudget(unit="tries", maximum_cost_minor=5)
    assert cycles.unit is UnitOfAccount.CYCLES
    assert tries.unit is UnitOfAccount.TRIES
    assert TIME_SCALE == "seconds"
    assert dump_budget_amount(unit="time", amount_minor=90) == {
        "unit": "time",
        "amount_minor": 90,
    }
    assert dump_budget_amount(unit="money", amount_minor=500, currency="USD") == {
        "currency": "USD",
        "amount_minor": 500,
    }


def test_mixing_money_fields_with_non_money_units_is_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="only valid when unit is money"):
        _policy(unit="tries", currency="USD")
    with pytest.raises(PydanticValidationError, match="only valid for money"):
        BudgetAmount(unit="time", currency="USD", amount_minor=60)
    with pytest.raises(PydanticValidationError, match="require an ISO 4217 currency"):
        BudgetAmount(unit="money", amount_minor=10)
    with pytest.raises(PydanticValidationError, match="only valid for money work"):
        WorkBudget(unit="cycles", currency="EUR", maximum_cost_minor=3)


def test_legacy_usd_payloads_remain_money() -> None:
    amount = BudgetAmount.model_validate({"currency": "USD", "amount_minor": 250})
    assert amount.unit is UnitOfAccount.MONEY
    assert identity_of({"currency": "USD", "amount_minor": 250}) == ("money", "USD")


def test_existing_usd_closed_beta_constitution_still_loads() -> None:
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    constitution = dict(manifest["company"]["constitution"])
    constitution.update(
        {
            "company_id": manifest["company"]["company_id"],
            "status": ConstitutionStatus.ACTIVE.value,
            "created_at": NOW.isoformat(),
            "created_by_principal_id": manifest["company"]["founder_principal_id"],
            "effective_at": NOW.isoformat(),
        }
    )
    loaded = Constitution.model_validate(constitution)
    assert loaded.budget_policy.unit is UnitOfAccount.MONEY
    assert loaded.budget_policy.currency == "USD"
    assert loaded.budget_policy.company_limit_minor == 100_000


def test_constitution_can_declare_time_or_tries() -> None:
    shared = {
        "constitution_id": uuid4(),
        "company_id": uuid4(),
        "version": 1,
        "status": ConstitutionStatus.DRAFT,
        "mission": "Budget attention and attempts, not cash.",
        "authority_policy": AuthorityPolicy(
            founder_principal_ids=frozenset({uuid4()}),
            irreversible_actions=frozenset({"constitution.activate"}),
            human_approval_actions=frozenset({"company.dissolve"}),
        ),
        "risk_policy": RiskPolicy(
            prohibited_actions=frozenset({"claim.fabricate"}),
            pricing_floor_minor=0,
            max_external_messages_per_experiment=10,
            regulated_market_entry_requires_founder=True,
        ),
        "fitness_policy": FitnessPolicy(
            required_gates=frozenset(GateName),
            minimum_evidence_count=1,
            minimum_customer_outcome_bps=7_000,
            minimum_delivery_quality_bps=8_000,
            allocation_cadence_days=7,
        ),
        "created_at": NOW,
        "created_by_principal_id": uuid4(),
    }
    time_constitution = Constitution(
        **shared,
        budget_policy=_policy(
            unit="time",
            currency=None,
            company_limit_minor=36_000,
            cell_limit_minor=18_000,
            experiment_limit_minor=3_600,
            single_action_limit_minor=1_800,
        ),
    )
    tries_constitution = Constitution(
        **{**shared, "constitution_id": uuid4()},
        budget_policy=_policy(
            unit="tries",
            currency=None,
            company_limit_minor=20,
            cell_limit_minor=10,
            experiment_limit_minor=4,
            single_action_limit_minor=2,
        ),
    )
    assert time_constitution.budget_policy.unit is UnitOfAccount.TIME
    assert tries_constitution.budget_policy.unit is UnitOfAccount.TRIES

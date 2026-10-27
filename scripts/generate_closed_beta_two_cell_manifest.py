#!/usr/bin/env python3
"""Deterministic sanitized two-cell closed-beta bootstrap example.

The output uses placeholder UUIDs derived from a stable namespace.  It contains
no provider IDs, users, live budgets, or business data from a real beta.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid5

ROOT = Path(__file__).resolve().parents[1]
AGENTS_ROOT = ROOT / "agents"
EXAMPLE_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
REQUIRED_GATES = [
    "legality_and_policy",
    "truthfulness_and_claim_support",
    "customer_outcome_floor",
    "security_and_privacy",
    "budget_and_cash",
    "delivery_quality_floor",
    "data_sufficiency_and_independence",
]


def example_uuid(label: str) -> str:
    return str(uuid5(EXAMPLE_NAMESPACE, f"ai-nature-company:closed-beta-example:{label}"))


def _definition(name: str) -> dict[str, object]:
    return json.loads((AGENTS_ROOT / name / "agent-definition.json").read_text(encoding="utf-8"))


def generate_two_cell_manifest() -> Mapping[str, object]:
    founder_id = example_uuid("founder")
    operating_cell_id = example_uuid("cell-operating")
    control_cell_id = example_uuid("cell-scope-control")
    owner_definition = _definition("cell-owner")
    specialist_definition = _definition("specialist")
    evaluator_definition = _definition("evaluator")
    return MappingProxyType(
        {
            "schema_version": 1,
            "company": {
                "company_id": example_uuid("company"),
                "founder_principal_id": founder_id,
                "name": "Example Closed Beta Company",
                "constitution": {
                    "schema_version": 1,
                    "constitution_id": example_uuid("constitution"),
                    "version": 1,
                    "mission": "Create verified customer value under founder control.",
                    "authority_policy": {
                        "founder_principal_ids": [founder_id],
                        "irreversible_actions": ["company.dissolve"],
                        "human_approval_actions": ["constitution.activate"],
                    },
                    "risk_policy": {
                        "prohibited_actions": ["claim.fabricate"],
                        "pricing_floor_minor": 0,
                        "max_external_messages_per_experiment": 10,
                        "regulated_market_entry_requires_founder": True,
                    },
                    "budget_policy": {
                        "currency": "USD",
                        "company_limit_minor": 100_000,
                        "cell_limit_minor": 50_000,
                        "experiment_limit_minor": 10_000,
                        "single_action_limit_minor": 10_000,
                        "exploration_share_bps": 2_000,
                    },
                    "fitness_policy": {
                        "rule_version": "fitness-v1",
                        "required_gates": REQUIRED_GATES,
                        "minimum_evidence_count": 1,
                        "minimum_customer_outcome_bps": 7_000,
                        "minimum_delivery_quality_bps": 8_000,
                        "allocation_cadence_days": 7,
                    },
                },
                "budget": {
                    "budget_id": example_uuid("company-budget"),
                    "limit": "1000.00",
                    "currency": "USD",
                    "duration_days": 365,
                },
            },
            "cells": [
                {
                    "cell_id": operating_cell_id,
                    "name": "Example Operating Cell",
                    "objective": "Operate one placeholder offer loop for identity canaries.",
                    "budget_id": example_uuid("operating-cell-budget"),
                    "budget_limit": "500.00",
                    "currency": "USD",
                    "budget_duration_days": 90,
                },
                {
                    "cell_id": control_cell_id,
                    "name": "Example Scope-Control Cell",
                    "objective": "Remain empty so cross-cell denial canaries stay meaningful.",
                    "budget_id": example_uuid("control-cell-budget"),
                    "budget_limit": "100.00",
                    "currency": "USD",
                    "budget_duration_days": 90,
                },
            ],
            "agents": [
                {
                    "principal_id": example_uuid("operating-owner-principal"),
                    "agent_instance_id": example_uuid("operating-owner-instance"),
                    "role": "cell_owner",
                    "cell_id": operating_cell_id,
                    "allowed_actions": list(owner_definition["declared_capabilities"]),
                    "definition": owner_definition,
                },
                {
                    "principal_id": example_uuid("operating-specialist-principal"),
                    "agent_instance_id": example_uuid("operating-specialist-instance"),
                    "role": "specialist",
                    "cell_id": operating_cell_id,
                    "allowed_actions": list(specialist_definition["declared_capabilities"]),
                    "definition": specialist_definition,
                },
                {
                    "principal_id": example_uuid("operating-evaluator-principal"),
                    "agent_instance_id": example_uuid("operating-evaluator-instance"),
                    "role": "evaluator",
                    "cell_id": operating_cell_id,
                    "allowed_actions": list(evaluator_definition["declared_capabilities"]),
                    "definition": evaluator_definition,
                },
                {
                    "principal_id": example_uuid("control-owner-principal"),
                    "agent_instance_id": example_uuid("control-owner-instance"),
                    "role": "cell_owner",
                    "cell_id": control_cell_id,
                    "allowed_actions": list(owner_definition["declared_capabilities"]),
                    "definition": owner_definition,
                },
            ],
        }
    )


def render_two_cell_manifest() -> str:
    return json.dumps(dict(generate_two_cell_manifest()), indent=2, sort_keys=True) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit the sanitized two-cell closed-beta bootstrap example.",
    )
    parser.add_argument(
        "--output",
        help="optional path to write the example manifest; defaults to stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rendered = render_two_cell_manifest()
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

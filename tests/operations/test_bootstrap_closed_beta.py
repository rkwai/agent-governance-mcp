from __future__ import annotations

import copy
import json
import stat
from pathlib import Path

import pytest

from ai_nature_company.store import InMemoryStore
from scripts.bootstrap_closed_beta import (
    DATABASE_URL_ENV,
    DATABASE_URL_FILE_ENV,
    MAX_MANIFEST_BYTES,
    OPERATOR_DATABASE_ROLE,
    BootstrapError,
    BootstrapManifest,
    apply_bootstrap,
    bootstrap_plan,
    database_url_from_environment,
    load_manifest,
    main,
    require_operator_role,
)

COMPANY_ID = "00000000-0000-4000-8000-000000000001"
FOUNDER_ID = "00000000-0000-4000-8000-000000000002"
CONSTITUTION_ID = "00000000-0000-4000-8000-000000000003"
COMPANY_BUDGET_ID = "00000000-0000-4000-8000-000000000004"
CELL_ID = "00000000-0000-4000-8000-000000000005"
CELL_BUDGET_ID = "00000000-0000-4000-8000-000000000006"
OWNER_PRINCIPAL_ID = "00000000-0000-4000-8000-000000000007"
OWNER_INSTANCE_ID = "00000000-0000-4000-8000-000000000008"
SPECIALIST_PRINCIPAL_ID = "00000000-0000-4000-8000-000000000009"
SPECIALIST_INSTANCE_ID = "00000000-0000-4000-8000-00000000000a"
ROOT = Path(__file__).resolve().parents[2]

REQUIRED_GATES = [
    "legality_and_policy",
    "truthfulness_and_claim_support",
    "customer_outcome_floor",
    "security_and_privacy",
    "budget_and_cash",
    "delivery_quality_floor",
    "data_sufficiency_and_independence",
]


def _definition(name: str) -> dict[str, object]:
    return json.loads(
        (ROOT / "agents" / name / "agent-definition.json").read_text(encoding="utf-8")
    )


def manifest() -> dict[str, object]:
    owner_definition = _definition("cell-owner")
    specialist_definition = _definition("specialist")
    return {
        "schema_version": 1,
        "company": {
            "company_id": COMPANY_ID,
            "founder_principal_id": FOUNDER_ID,
            "name": "Closed Beta Company",
            "constitution": {
                "schema_version": 1,
                "constitution_id": CONSTITUTION_ID,
                "version": 1,
                "mission": "Create verified customer value under founder control.",
                "authority_policy": {
                    "founder_principal_ids": [FOUNDER_ID],
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
                "budget_id": COMPANY_BUDGET_ID,
                "limit": "1000.00",
                "currency": "USD",
                "duration_days": 365,
            },
        },
        "cells": [
            {
                "cell_id": CELL_ID,
                "name": "Cell Alpha",
                "objective": "Find a profitable customer-value loop.",
                "budget_id": CELL_BUDGET_ID,
                "budget_limit": "500.00",
                "currency": "USD",
                "budget_duration_days": 90,
            }
        ],
        "agents": [
            {
                "principal_id": OWNER_PRINCIPAL_ID,
                "agent_instance_id": OWNER_INSTANCE_ID,
                "role": "cell_owner",
                "cell_id": CELL_ID,
                "allowed_actions": list(owner_definition["declared_capabilities"]),
                "definition": owner_definition,
            },
            {
                "principal_id": SPECIALIST_PRINCIPAL_ID,
                "agent_instance_id": SPECIALIST_INSTANCE_ID,
                "role": "specialist",
                "cell_id": CELL_ID,
                "allowed_actions": list(specialist_definition["declared_capabilities"]),
                "definition": specialist_definition,
            },
        ],
    }


def spec() -> BootstrapManifest:
    return BootstrapManifest.from_mapping(manifest())


def test_plan_is_versioned_non_secret_and_orders_domain_authority_before_identity() -> None:
    desired = spec()
    plan = bootstrap_plan(desired)
    serialized = json.dumps(dict(plan))

    assert plan["schema_version"] == 1
    assert plan["manifest_sha256"] == desired.digest
    assert plan["database_role"] == OPERATOR_DATABASE_ROLE
    assert plan["identity_activation"] == "not-performed"
    assert [action["tool"] for action in plan["actions"]] == [
        "company.bootstrap",
        "cell.create",
        "agent.register",
        "agent.register",
    ]
    assert "database_url" not in serialized.casefold()
    assert "client_secret" not in serialized.casefold()
    assert "access_token" not in serialized.casefold()


def test_semantic_collection_order_has_one_stable_digest_and_plan() -> None:
    first = manifest()
    second = copy.deepcopy(first)
    second["agents"] = list(reversed(second["agents"]))
    first_spec = BootstrapManifest.from_mapping(first)
    second_spec = BootstrapManifest.from_mapping(second)

    assert first_spec.digest == second_spec.digest
    assert bootstrap_plan(first_spec) == bootstrap_plan(second_spec)


def test_every_closed_beta_cell_requires_exactly_one_accountable_owner() -> None:
    missing = manifest()
    missing["agents"] = missing["agents"][1:]
    with pytest.raises(BootstrapError, match="exactly one cell_owner"):
        BootstrapManifest.from_mapping(missing)

    duplicate = manifest()
    owner_definition = _definition("cell-owner")
    duplicate["agents"][1].update(
        {
            "role": "cell_owner",
            "allowed_actions": list(owner_definition["declared_capabilities"]),
            "definition": owner_definition,
        }
    )
    with pytest.raises(BootstrapError, match="exactly one cell_owner"):
        BootstrapManifest.from_mapping(duplicate)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update({"client_secret": "not-allowed"}),
            "prohibited secret field",
        ),
        (
            lambda value: value["company"]["budget"].update({"limit": "999.00"}),
            "must equal constitution",
        ),
        (
            lambda value: value["cells"][0].update({"budget_duration_days": 365}),
            "must be shorter",
        ),
        (
            lambda value: value["agents"][0].update(
                {"cell_id": "00000000-0000-4000-8000-000000000099"}
            ),
            "does not identify",
        ),
        (
            lambda value: value["agents"][0].update({"role": "evaluator"}),
            "incompatible",
        ),
        (
            lambda value: value["agents"][0].update({"cell_id": None}),
            "cell_id is required",
        ),
        (
            lambda value: value["agents"][1].update({"principal_id": OWNER_PRINCIPAL_ID}),
            "principal_id values must be unique",
        ),
        (
            lambda value: value["agents"][0]["allowed_actions"].append("constitution.amend"),
            "exceed definition",
        ),
    ],
)
def test_manifest_fails_closed_before_database_access(mutate: object, message: str) -> None:
    value = manifest()
    mutate(value)

    with pytest.raises(BootstrapError, match=message):
        BootstrapManifest.from_mapping(value)


async def test_apply_uses_only_canonical_mutations_and_is_exactly_replay_safe() -> None:
    desired = spec()
    store = InMemoryStore()

    first = await apply_bootstrap(desired, store)
    first_export = await store.export_company(COMPANY_ID)
    second = await apply_bootstrap(desired, store)
    second_export = await store.export_company(COMPANY_ID)

    assert first["status"] == "verified"
    assert first["company_replayed"] is False
    assert first["replayed_cells"] == 0
    assert first["replayed_agents"] == 0
    assert second["company_replayed"] is True
    assert second["replayed_cells"] == 1
    assert second["replayed_agents"] == 2
    assert second["identity_activation"] == "ready-not-performed"
    assert {key: value for key, value in first_export.items() if key != "exported_at"} == {
        key: value for key, value in second_export.items() if key != "exported_at"
    }

    projections = first_export["projections"]
    projection_types = [record["projection_type"] for record in projections]
    assert projection_types.count("company") == 1
    assert projection_types.count("constitution") == 1
    assert projection_types.count("cell") == 1
    assert projection_types.count("agent_definition") == 2
    assert projection_types.count("agent") == 2
    assert projection_types.count("agent_binding") == 2
    assert [event["event_type"] for event in first_export["domain_events"]] == [
        "company.bootstrapped",
        "constitution.activated",
        "cell.created",
        "agent.registered",
        "agent.registered",
    ]
    assert len(first_export["audit_events"]) == 4


async def test_exact_verification_rejects_post_bootstrap_projection_drift() -> None:
    desired = spec()
    store = InMemoryStore()
    await apply_bootstrap(desired, store)

    async with store.transaction(COMPANY_ID) as transaction:
        record = await transaction.get_projection("agent", OWNER_INSTANCE_ID)
        assert record is not None
        changed = dict(record["state"])
        changed["status"] = "suspended"
        await transaction.put_projection(
            projection_type="agent",
            projection_id=OWNER_INSTANCE_ID,
            state=changed,
            expected_revision=record["revision"],
        )

    with pytest.raises(BootstrapError, match="unexpected revision"):
        await apply_bootstrap(desired, store)


@pytest.mark.parametrize(
    "row",
    [
        None,
        (
            "ai_nature_company_runtime",
            True,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
        ),
        (OPERATOR_DATABASE_ROLE, False, False, False, False, False, False, False, False),
        (OPERATOR_DATABASE_ROLE, True, True, False, False, False, False, False, False),
        (OPERATOR_DATABASE_ROLE, True, False, False, False, False, False, True, False),
        (OPERATOR_DATABASE_ROLE, True, False, False, False, False, False, False, True),
    ],
)
def test_operator_role_check_refuses_every_unsafe_authority(row: object) -> None:
    with pytest.raises(BootstrapError):
        require_operator_role(row)


def test_operator_role_check_accepts_only_exact_safe_role() -> None:
    require_operator_role(
        (OPERATOR_DATABASE_ROLE, True, False, False, False, False, False, False, False)
    )


def test_operator_database_url_uses_one_environment_source_or_owned_0600_file(
    tmp_path: Path,
) -> None:
    dsn = "postgresql://operator@example.test/company?sslmode=verify-full&sslrootcert=system"
    assert database_url_from_environment({DATABASE_URL_ENV: dsn}) == dsn
    with pytest.raises(BootstrapError, match="exactly one"):
        database_url_from_environment({})
    with pytest.raises(BootstrapError, match="exactly one"):
        database_url_from_environment(
            {DATABASE_URL_ENV: dsn, DATABASE_URL_FILE_ENV: str(tmp_path / "dsn")}
        )

    path = tmp_path / "operator-dsn"
    path.write_text(f"{dsn}\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert database_url_from_environment({DATABASE_URL_FILE_ENV: str(path)}) == dsn
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(BootstrapError, match="0600"):
        database_url_from_environment({DATABASE_URL_FILE_ENV: str(path)})


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://operator@example.test/company",
        ("postgresql://operator@example.test:6543/company?sslmode=verify-full&sslrootcert=system"),
        ("postgresql://operator@example.test/company?sslmode=require&sslrootcert=system"),
        ("postgresql://operator@example.test/company?sslmode=verify-full&sslrootcert=relative.crt"),
    ],
)
def test_operator_database_url_rejects_insecure_connections(dsn: str) -> None:
    with pytest.raises(BootstrapError):
        database_url_from_environment({DATABASE_URL_ENV: dsn})


def test_manifest_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")

    with pytest.raises(BootstrapError, match="duplicate JSON key"):
        load_manifest(str(path))


def test_manifest_loader_bounds_file_reads(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * (MAX_MANIFEST_BYTES + 1))

    with pytest.raises(BootstrapError, match=str(MAX_MANIFEST_BYTES)):
        load_manifest(str(path))


def test_plan_cli_never_requires_or_prints_database_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps(manifest()), encoding="utf-8")

    assert main(["plan", "--manifest", str(path)]) == 0
    output = capsys.readouterr()
    plan = json.loads(output.out)
    assert plan["identity_activation"] == "not-performed"
    assert "postgresql://" not in output.out
    assert output.err == ""

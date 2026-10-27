from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from ai_nature_company.auth import Role
from ai_nature_company.policy import ROLE_CAPABILITIES
from ai_nature_company.server import PHASE_ONE_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[2]
DEFINITION_PATHS = sorted((ROOT / "agents").glob("*/agent-definition.json"))
SIGNED_DEFINITIONS = {
    "allocator",
    "cell-owner",
    "delivery",
    "evaluator",
    "market-scout",
    "memory",
    "sales",
}
KERNEL_LEFTOVER_DEFINITIONS = {"specialist", "immune"}
EXPECTED_ROLES = {"cell_owner", "specialist", "evaluator", "allocator", "immune"}
SIGNED_ROLE_BY_DEFINITION = {
    "allocator": "allocator",
    "cell-owner": "cell_owner",
    "delivery": "specialist",
    "evaluator": "evaluator",
    "market-scout": "specialist",
    "memory": "specialist",
    "sales": "specialist",
}
EXPOSED_PHASE1_CAPABILITIES = {
    "agent.get",
    "agent.list",
    "agent.register",
    "allocation.commit",
    "allocation.propose",
    "audit.list",
    "cell.amend",
    "cell.create",
    "cell.get",
    "cell.list",
    "cell.transition",
    "company.bootstrap",
    "company.describe",
    "constitution.get",
    "constitution.amend",
    "decision.explain",
    "economics.append",
    "evaluation.submit",
    "evidence.append",
    "experiment.amend",
    "experiment.close",
    "experiment.record_send",
    "experiment.register",
    "experiment.start",
    "gate.evaluate",
    "maintenance.run",
    "learning.promote",
    "learning.propose",
    "offer.mutate",
    "offer.register",
    "protocol.describe",
    "work.cancel",
    "work.claim",
    "work.complete",
    "work.create",
    "work.fail",
    "work.heartbeat",
    "work.release",
}


def definitions() -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in DEFINITION_PATHS]


def definitions_by_directory() -> dict[str, dict[str, Any]]:
    return {
        path.parent.name: json.loads(path.read_text(encoding="utf-8")) for path in DEFINITION_PATHS
    }


def test_agent_definitions_are_valid_unique_data_configuration() -> None:
    schema = json.loads((ROOT / "schemas" / "agent-definition-v1.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    manifests = definitions()
    by_directory = definitions_by_directory()

    present = {path.parent.name for path in DEFINITION_PATHS}
    assert present >= SIGNED_DEFINITIONS
    assert present >= KERNEL_LEFTOVER_DEFINITIONS
    assert {manifest["compatible_roles"][0] for manifest in manifests} == EXPECTED_ROLES
    assert len({manifest["definition_id"] for manifest in manifests}) == len(manifests)
    assert len({manifest["name"] for manifest in manifests}) == len(manifests)
    for kind, role in SIGNED_ROLE_BY_DEFINITION.items():
        assert by_directory[kind]["compatible_roles"] == [role]

    for manifest in manifests:
        validator.validate(manifest)
        assert len(manifest["compatible_roles"]) == 1
        assert set(manifest["declared_capabilities"]) <= EXPOSED_PHASE1_CAPABILITIES
        assert manifest["constraints"]["may_self_evaluate"] is False
        assert manifest["constraints"]["may_change_constitution"] is False
        forbidden_keys = {"api_key", "credential", "model", "provider", "secret", "token"}
        assert not (_keys(manifest) & forbidden_keys)

        for contract_name in ("input_contract", "output_contract"):
            schema_ref = manifest[contract_name]["schema_ref"]
            assert (ROOT / schema_ref).is_file()


def test_manifest_capability_allowlist_matches_exposed_mcp_surface() -> None:
    assert set(PHASE_ONE_TOOL_NAMES) == EXPOSED_PHASE1_CAPABILITIES


def test_manifests_declare_only_operations_available_to_their_compatible_role() -> None:
    for manifest in definitions():
        role = Role(manifest["compatible_roles"][0])
        granted = {capability.value for capability in ROLE_CAPABILITIES[role]}
        public_grants = {
            tool_name
            for tool_name in EXPOSED_PHASE1_CAPABILITIES
            if tool_name in granted
            or any(capability.startswith(f"{tool_name}.") for capability in granted)
        }
        assert set(manifest["declared_capabilities"]) <= public_grants


def test_agent_roles_have_conflict_separation() -> None:
    by_directory = definitions_by_directory()

    assert "evaluation.submit" not in by_directory["cell-owner"]["declared_capabilities"]
    assert "allocation.commit" in by_directory["cell-owner"]["declared_capabilities"]
    assert by_directory["cell-owner"]["constraints"]["may_commit_allocation"] is True

    for kind in ("specialist", "market-scout", "sales", "delivery", "memory"):
        assert "evaluation.submit" not in by_directory[kind]["declared_capabilities"]
        assert "allocation.commit" not in by_directory[kind]["declared_capabilities"]
        assert by_directory[kind]["constraints"]["may_self_evaluate"] is False

    assert "learning.promote" not in by_directory["memory"]["declared_capabilities"]
    assert "learning.propose" in by_directory["memory"]["declared_capabilities"]

    assert "experiment.start" not in by_directory["evaluator"]["declared_capabilities"]
    assert "experiment.amend" not in by_directory["evaluator"]["declared_capabilities"]
    assert "experiment.amend" in by_directory["cell-owner"]["declared_capabilities"]
    assert "cell.amend" not in by_directory["evaluator"]["declared_capabilities"]
    assert "cell.amend" in by_directory["cell-owner"]["declared_capabilities"]
    assert "experiment.record_send" not in by_directory["evaluator"]["declared_capabilities"]
    assert "experiment.record_send" in by_directory["cell-owner"]["declared_capabilities"]
    assert "work.cancel" in by_directory["cell-owner"]["declared_capabilities"]
    assert "work.cancel" not in by_directory["evaluator"]["declared_capabilities"]
    assert "audit.list" in by_directory["cell-owner"]["declared_capabilities"]
    assert "evidence.append" not in by_directory["evaluator"]["declared_capabilities"]
    assert "allocation.commit" not in by_directory["evaluator"]["declared_capabilities"]

    assert "evaluation.submit" not in by_directory["allocator"]["declared_capabilities"]
    assert "experiment.start" not in by_directory["allocator"]["declared_capabilities"]
    assert by_directory["allocator"]["constraints"]["may_commit_allocation"] is True

    assert "experiment.start" not in by_directory["immune"]["declared_capabilities"]
    assert "evaluation.submit" not in by_directory["immune"]["declared_capabilities"]
    assert "allocation.commit" not in by_directory["immune"]["declared_capabilities"]


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys |= _keys(child)
        return keys
    if isinstance(value, list):
        keys = set()
        for child in value:
            keys |= _keys(child)
        return keys
    return set()

"""Acceptance contracts for checked-in, model-neutral agent definitions."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator, FormatChecker

from .support import AGENTS_ROOT, ROOT


def test_ac02_required_versioned_agent_definitions_are_valid() -> None:
    schema = json.loads((ROOT / "schemas" / "agent-definition-v1.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    required = {
        "allocator",
        "cell-owner",
        "delivery",
        "evaluator",
        "market-scout",
        "memory",
        "sales",
    }
    present = {path.parent.name for path in AGENTS_ROOT.glob("*/agent-definition.json")}

    assert present >= required
    for kind in sorted(present):
        path = AGENTS_ROOT / kind / "agent-definition.json"
        definition = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(definition), key=lambda error: list(error.path))
        assert errors == [], f"{path}: {[error.message for error in errors]}"
        assert definition["schema_version"] == 1
        assert definition["version"] >= 1
        assert definition["declared_capabilities"]
        assert "provider" not in definition
        assert "model" not in definition
        assert "credentials" not in definition

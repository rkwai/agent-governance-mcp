#!/usr/bin/env python3
"""Bootstrap closed-beta domain authority through the canonical service boundary.

The script intentionally does not write projections or identity tables directly.
It creates the company, cells, reusable agent definitions, concrete agent instances,
and principal bindings by invoking the same deterministic mutations used by MCP.
Supabase identity activation is a separate, later provisioning step.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError as PydanticValidationError

from ai_nature_company.auth import (
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
)
from ai_nature_company.database_url import require_tls_database_url
from ai_nature_company.ledger_units import MONEY, UNITS
from ai_nature_company.models import AgentDefinition, Constitution, ConstitutionStatus
from ai_nature_company.postgres import PostgresRepository
from ai_nature_company.service import SubstrateService, create_service
from ai_nature_company.store import Repository

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 1_048_576
MAX_COLLECTION_SIZE = 1_000
OPERATOR_DATABASE_ROLE = "ai_nature_company_operator"
DATABASE_URL_ENV = "ANC_OPERATOR_DATABASE_URL"
DATABASE_URL_FILE_ENV = "ANC_OPERATOR_DATABASE_URL_FILE"
_MONEY_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_PROHIBITED_MANIFEST_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "database_url",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "service_role_key",
        "token",
    }
)
_AGENT_ROLES = frozenset(
    {
        Role.CELL_OWNER,
        Role.SPECIALIST,
        Role.EVALUATOR,
        Role.ALLOCATOR,
        Role.IMMUNE,
        Role.OBSERVER,
    }
)
_CELL_SCOPED_ROLES = frozenset({Role.CELL_OWNER, Role.SPECIALIST, Role.EVALUATOR})


class BootstrapError(ValueError):
    """The bootstrap manifest, database role, or stored state is unsafe."""


def _plain(value: Any) -> Any:
    """Return deterministic JSON-compatible data for hashing and comparison."""

    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in sorted(value.items())}
    if isinstance(value, (set, frozenset)):
        normalized = [_plain(child) for child in value]
        return sorted(
            normalized,
            key=lambda child: json.dumps(child, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise BootstrapError("stored state contains a naive timestamp")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise BootstrapError(f"{field_name} must be an object with string keys")
    return value


def _exact_fields(value: Mapping[str, object], expected: set[str], field_name: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing:
        raise BootstrapError(f"{field_name} is missing fields: {', '.join(missing)}")
    if unknown:
        raise BootstrapError(f"{field_name} contains unknown fields: {', '.join(unknown)}")


def _identifier(value: object, field_name: str, *, maximum: int = 4_000) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BootstrapError(f"{field_name} must be a non-empty canonical string")
    if len(value) > maximum:
        raise BootstrapError(f"{field_name} exceeds {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BootstrapError(f"{field_name} cannot contain control characters")
    return value


def _uuid(value: object, field_name: str) -> str:
    canonical = _identifier(value, field_name, maximum=36)
    try:
        parsed = UUID(canonical)
    except (AttributeError, TypeError, ValueError) as error:
        raise BootstrapError(f"{field_name} must be a canonical UUID") from error
    if str(parsed) != canonical:
        raise BootstrapError(f"{field_name} must be a canonical UUID")
    return canonical


def _integer(value: object, field_name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BootstrapError(f"{field_name} must be an integer of at least {minimum}")
    return value


def _money(value: object, field_name: str) -> tuple[str, int]:
    if not isinstance(value, str) or _MONEY_PATTERN.fullmatch(value) is None:
        raise BootstrapError(f"{field_name} must be a canonical non-negative two-decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:  # pragma: no cover - guarded by the regular expression
        raise BootstrapError(f"{field_name} is not a decimal amount") from error
    return value, int(amount * 100)


def _currency(value: object, field_name: str) -> str:
    canonical = _identifier(value, field_name, maximum=3)
    if _CURRENCY_PATTERN.fullmatch(canonical) is None:
        raise BootstrapError(f"{field_name} must be an uppercase ISO-style currency code")
    return canonical


def _unit(value: object, field_name: str) -> str:
    canonical = _identifier(value, field_name, maximum=16)
    if canonical not in UNITS:
        raise BootstrapError(f"{field_name} must be money, time, cycles, or tries")
    return canonical


def _quantity_limit(value: object, unit: str, field_name: str) -> tuple[str | int, int]:
    if unit == MONEY:
        return _money(value, field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BootstrapError(f"{field_name} must be a whole {unit} quantity")
    return value, value


def _list(value: object, field_name: str, *, require_items: bool = True) -> list[object]:
    if not isinstance(value, list):
        raise BootstrapError(f"{field_name} must be a list")
    if require_items and not value:
        raise BootstrapError(f"{field_name} must not be empty")
    if len(value) > MAX_COLLECTION_SIZE:
        raise BootstrapError(f"{field_name} exceeds {MAX_COLLECTION_SIZE} items")
    return value


def _reject_secret_fields(value: object, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            normalized = str(raw_key).casefold().replace("-", "_").replace(" ", "_")
            if normalized in _PROHIBITED_MANIFEST_KEYS:
                raise BootstrapError(f"{path} contains prohibited secret field {raw_key!r}")
            _reject_secret_fields(child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


@dataclass(frozen=True, slots=True)
class CompanyBootstrapSpec:
    company_id: str
    founder_principal_id: str
    name: str
    constitution: Mapping[str, object]
    budget: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CellBootstrapSpec:
    cell_id: str
    name: str
    objective: str
    budget_id: str
    budget_limit: str | int
    budget_limit_minor: int
    currency: str | None
    unit: str
    budget_duration_days: int

    @property
    def payload(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "cell_id": self.cell_id,
            "name": self.name,
            "objective": self.objective,
            "budget_id": self.budget_id,
            "budget_limit": self.budget_limit,
            "budget_duration_days": self.budget_duration_days,
        }
        if self.unit == MONEY:
            payload["currency"] = self.currency
        else:
            payload["unit"] = self.unit
        return MappingProxyType(payload)


@dataclass(frozen=True, slots=True)
class AgentBootstrapSpec:
    principal_id: str
    agent_instance_id: str
    role: Role
    cell_id: str | None
    allowed_actions: tuple[str, ...]
    definition: Mapping[str, object]
    definition_id: str
    definition_version: int

    @property
    def payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "principal_id": self.principal_id,
                "agent_instance_id": self.agent_instance_id,
                "role": self.role.value,
                "cell_id": self.cell_id,
                "allowed_actions": list(self.allowed_actions),
                "definition": dict(self.definition),
            }
        )


@dataclass(frozen=True, slots=True)
class BootstrapManifest:
    schema_version: int
    company: CompanyBootstrapSpec
    cells: tuple[CellBootstrapSpec, ...]
    agents: tuple[AgentBootstrapSpec, ...]
    digest: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> BootstrapManifest:
        """Validate and normalize one complete, non-secret desired-state manifest."""

        _reject_secret_fields(raw)
        _exact_fields(raw, {"schema_version", "company", "cells", "agents"}, "manifest")
        if raw["schema_version"] != MANIFEST_VERSION:
            raise BootstrapError(f"manifest schema_version must be {MANIFEST_VERSION}")

        company_raw = _mapping(raw["company"], "company")
        _exact_fields(
            company_raw,
            {"company_id", "founder_principal_id", "name", "constitution", "budget"},
            "company",
        )
        company_id = _uuid(company_raw["company_id"], "company.company_id")
        founder_id = _uuid(
            company_raw["founder_principal_id"],
            "company.founder_principal_id",
        )
        name = _identifier(company_raw["name"], "company.name", maximum=250)

        constitution_raw = _mapping(company_raw["constitution"], "company.constitution")
        _exact_fields(
            constitution_raw,
            {
                "schema_version",
                "constitution_id",
                "version",
                "mission",
                "authority_policy",
                "risk_policy",
                "budget_policy",
                "fitness_policy",
            },
            "company.constitution",
        )
        validation_time = datetime(2000, 1, 1, tzinfo=UTC)
        constitution_candidate = dict(constitution_raw)
        constitution_candidate.update(
            company_id=company_id,
            status=ConstitutionStatus.ACTIVE.value,
            created_at=validation_time,
            created_by_principal_id=founder_id,
            effective_at=validation_time,
        )
        try:
            constitution_model = Constitution.model_validate(constitution_candidate)
        except PydanticValidationError as error:
            raise BootstrapError(
                "company.constitution is not a valid active constitution"
            ) from error
        if constitution_model.version != 1:
            raise BootstrapError("a fresh bootstrap constitution must have version 1")
        if constitution_model.authority_policy.founder_principal_ids != frozenset(
            {UUID(founder_id)}
        ):
            raise BootstrapError(
                "constitution founder_principal_ids must contain exactly founder_principal_id"
            )
        constitution = _plain(constitution_raw)

        budget_raw = _mapping(company_raw["budget"], "company.budget")
        company_unit = (
            _unit(budget_raw["unit"], "company.budget.unit")
            if "unit" in budget_raw
            else constitution_model.budget_policy.unit.value
        )
        money_budget_fields = {"budget_id", "limit", "currency", "duration_days"}
        if company_unit == MONEY:
            expected_budget_fields = set(money_budget_fields)
            if "unit" in budget_raw:
                expected_budget_fields.add("unit")
            _exact_fields(budget_raw, expected_budget_fields, "company.budget")
            company_currency = _currency(budget_raw["currency"], "company.budget.currency")
        else:
            _exact_fields(
                budget_raw,
                {"budget_id", "limit", "unit", "duration_days"},
                "company.budget",
            )
            company_currency = None
        company_budget_id = _uuid(budget_raw["budget_id"], "company.budget.budget_id")
        company_limit, company_limit_minor = _quantity_limit(
            budget_raw["limit"], company_unit, "company.budget.limit"
        )
        company_duration = _integer(budget_raw["duration_days"], "company.budget.duration_days")
        if company_limit_minor != constitution_model.budget_policy.company_limit_minor:
            raise BootstrapError(
                "company budget limit must equal constitution budget_policy.company_limit_minor"
            )
        if (
            company_unit != constitution_model.budget_policy.unit.value
            or company_currency != constitution_model.budget_policy.currency
        ):
            raise BootstrapError("company budget unit must equal constitution budget_policy unit")
        budget_payload: dict[str, object] = {
            "budget_id": company_budget_id,
            "limit": company_limit,
            "duration_days": company_duration,
        }
        if company_unit == MONEY:
            budget_payload["currency"] = company_currency
        else:
            budget_payload["unit"] = company_unit
        budget = MappingProxyType(budget_payload)

        cells: list[CellBootstrapSpec] = []
        seen_cell_ids: set[str] = set()
        seen_budget_ids = {
            company_budget_id,
            str(uuid5(UUID(company_budget_id), "stage:exploration")),
            str(uuid5(UUID(company_budget_id), "stage:exploitation")),
        }
        for index, item in enumerate(_list(raw["cells"], "cells")):
            field = f"cells[{index}]"
            cell_raw = _mapping(item, field)
            cell_unit = (
                _unit(cell_raw["unit"], f"{field}.unit") if "unit" in cell_raw else company_unit
            )
            money_cell_fields = {
                "cell_id",
                "name",
                "objective",
                "budget_id",
                "budget_limit",
                "currency",
                "budget_duration_days",
            }
            if cell_unit == MONEY:
                expected_cell_fields = set(money_cell_fields)
                if "unit" in cell_raw:
                    expected_cell_fields.add("unit")
                _exact_fields(cell_raw, expected_cell_fields, field)
            else:
                _exact_fields(
                    cell_raw,
                    {
                        "cell_id",
                        "name",
                        "objective",
                        "budget_id",
                        "budget_limit",
                        "unit",
                        "budget_duration_days",
                    },
                    field,
                )
            cell_id = _uuid(cell_raw["cell_id"], f"{field}.cell_id")
            if cell_id in seen_cell_ids:
                raise BootstrapError("cell_id values must be unique")
            seen_cell_ids.add(cell_id)
            cell_budget_id = _uuid(cell_raw["budget_id"], f"{field}.budget_id")
            if cell_budget_id in seen_budget_ids:
                raise BootstrapError("company, stage, and cell budget_id values must be unique")
            seen_budget_ids.add(cell_budget_id)
            cell_limit, cell_limit_minor = _quantity_limit(
                cell_raw["budget_limit"], cell_unit, f"{field}.budget_limit"
            )
            if cell_limit_minor > constitution_model.budget_policy.cell_limit_minor:
                raise BootstrapError(
                    "cell budget exceeds constitution budget_policy.cell_limit_minor"
                )
            if cell_limit_minor > company_limit_minor:
                raise BootstrapError("cell budget exceeds company budget limit")
            cell_currency = (
                _currency(cell_raw["currency"], f"{field}.currency") if cell_unit == MONEY else None
            )
            if cell_unit != company_unit or cell_currency != company_currency:
                raise BootstrapError("cell budget unit must equal company budget unit")
            cell_duration = _integer(
                cell_raw["budget_duration_days"],
                f"{field}.budget_duration_days",
            )
            if cell_duration >= company_duration:
                raise BootstrapError(
                    "cell budget_duration_days must be shorter than company duration_days"
                )
            cells.append(
                CellBootstrapSpec(
                    cell_id=cell_id,
                    name=_identifier(cell_raw["name"], f"{field}.name", maximum=250),
                    objective=_identifier(
                        cell_raw["objective"], f"{field}.objective", maximum=4_000
                    ),
                    budget_id=cell_budget_id,
                    budget_limit=cell_limit,
                    budget_limit_minor=cell_limit_minor,
                    currency=cell_currency,
                    unit=cell_unit,
                    budget_duration_days=cell_duration,
                )
            )

        agents: list[AgentBootstrapSpec] = []
        seen_principals = {founder_id}
        seen_instances: set[str] = set()
        definitions: dict[str, AgentDefinition] = {}
        for index, item in enumerate(_list(raw["agents"], "agents")):
            field = f"agents[{index}]"
            agent_raw = _mapping(item, field)
            _exact_fields(
                agent_raw,
                {
                    "principal_id",
                    "agent_instance_id",
                    "role",
                    "cell_id",
                    "allowed_actions",
                    "definition",
                },
                field,
            )
            principal_id = _uuid(agent_raw["principal_id"], f"{field}.principal_id")
            if principal_id in seen_principals:
                raise BootstrapError("founder and agent principal_id values must be unique")
            seen_principals.add(principal_id)
            instance_id = _uuid(agent_raw["agent_instance_id"], f"{field}.agent_instance_id")
            if instance_id in seen_instances:
                raise BootstrapError("agent_instance_id values must be unique")
            seen_instances.add(instance_id)
            try:
                role = Role(_identifier(agent_raw["role"], f"{field}.role", maximum=32))
            except ValueError as error:
                raise BootstrapError(f"{field}.role is unknown") from error
            if role not in _AGENT_ROLES:
                raise BootstrapError(f"{field}.role must be a non-founder agent role")
            raw_cell_id = agent_raw["cell_id"]
            cell_id = _uuid(raw_cell_id, f"{field}.cell_id") if raw_cell_id is not None else None
            if role in _CELL_SCOPED_ROLES and cell_id is None:
                raise BootstrapError(f"{field}.cell_id is required for role {role.value}")
            if cell_id is not None and cell_id not in seen_cell_ids:
                raise BootstrapError(f"{field}.cell_id does not identify a manifest cell")

            definition_raw = _mapping(agent_raw["definition"], f"{field}.definition")
            try:
                definition_model = AgentDefinition.model_validate(definition_raw)
            except PydanticValidationError as error:
                raise BootstrapError(f"{field}.definition is invalid") from error
            compatible = {compatible.value for compatible in definition_model.compatible_roles}
            if role.value not in compatible:
                raise BootstrapError(f"{field}.role is incompatible with its definition")
            definition_id = str(definition_model.definition_id)
            existing_definition = definitions.get(definition_id)
            if existing_definition is not None and existing_definition != definition_model:
                raise BootstrapError(
                    "one definition_id cannot identify different immutable definitions"
                )
            definitions[definition_id] = definition_model

            raw_actions = _list(agent_raw["allowed_actions"], f"{field}.allowed_actions")
            actions = tuple(
                _identifier(action, f"{field}.allowed_actions", maximum=128)
                for action in raw_actions
            )
            if len(set(actions)) != len(actions):
                raise BootstrapError(f"{field}.allowed_actions must not contain duplicates")
            declared = set(definition_model.declared_capabilities)
            if not set(actions) <= declared:
                raise BootstrapError(f"{field}.allowed_actions exceed definition capabilities")

            agents.append(
                AgentBootstrapSpec(
                    principal_id=principal_id,
                    agent_instance_id=instance_id,
                    role=role,
                    cell_id=cell_id,
                    allowed_actions=tuple(sorted(actions)),
                    definition=MappingProxyType(_plain(definition_model)),
                    definition_id=definition_id,
                    definition_version=definition_model.version,
                )
            )

        owner_counts = {cell_id: 0 for cell_id in seen_cell_ids}
        for agent in agents:
            if agent.role is Role.CELL_OWNER:
                if agent.cell_id is None:  # Defensive; validated above before construction.
                    raise BootstrapError("cell_owner requires a manifest cell")
                owner_counts[agent.cell_id] += 1
        invalid_owner_cells = sorted(
            cell_id for cell_id, count in owner_counts.items() if count != 1
        )
        if invalid_owner_cells:
            raise BootstrapError(
                "every manifest cell must have exactly one cell_owner agent instance"
            )

        company = CompanyBootstrapSpec(
            company_id=company_id,
            founder_principal_id=founder_id,
            name=name,
            constitution=MappingProxyType(dict(constitution)),
            budget=budget,
        )
        normalized_cells = tuple(sorted(cells, key=lambda item: item.cell_id))
        normalized_agents = tuple(sorted(agents, key=lambda item: item.agent_instance_id))
        normalized = {
            "schema_version": MANIFEST_VERSION,
            "company": {
                "company_id": company.company_id,
                "founder_principal_id": company.founder_principal_id,
                "name": company.name,
                "constitution": company.constitution,
                "budget": company.budget,
            },
            "cells": [cell.payload for cell in normalized_cells],
            "agents": [agent.payload for agent in normalized_agents],
        }
        digest = hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
        return cls(
            schema_version=MANIFEST_VERSION,
            company=company,
            cells=normalized_cells,
            agents=normalized_agents,
            digest=digest,
        )

    @property
    def company_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "company_id": self.company.company_id,
                "name": self.company.name,
                "constitution": dict(self.company.constitution),
                "budget": dict(self.company.budget),
                "agents": [],
            }
        )

    def public_summary(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "manifest_sha256": self.digest,
                "company_id": self.company.company_id,
                "founder_principal_id": self.company.founder_principal_id,
                "cell_ids": [cell.cell_id for cell in self.cells],
                "agents": [
                    {
                        "principal_id": agent.principal_id,
                        "agent_instance_id": agent.agent_instance_id,
                        "definition_id": agent.definition_id,
                        "definition_version": agent.definition_version,
                        "role": agent.role.value,
                        "cell_id": agent.cell_id,
                    }
                    for agent in self.agents
                ],
            }
        )


def _operation_key(manifest: BootstrapManifest, tool_name: str, target: str) -> str:
    return f"closed-beta-v1:{manifest.digest}:{tool_name}:{target}"


def _mutation(
    manifest: BootstrapManifest,
    tool_name: str,
    target: str,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    correlation_id = str(
        uuid5(
            NAMESPACE_URL,
            f"ai-nature-company:closed-beta:{manifest.digest}:{tool_name}:{target}",
        )
    )
    return MappingProxyType(
        {
            "idempotency_key": _operation_key(manifest, tool_name, target),
            "expected_revision": 0,
            "correlation_id": correlation_id,
            "operation_intent": tool_name,
            "payload": dict(payload),
        }
    )


def bootstrap_plan(manifest: BootstrapManifest) -> Mapping[str, object]:
    """Return the ordered non-secret mutation plan without touching the database."""

    actions: list[dict[str, object]] = [
        {
            "order": 1,
            "tool": "company.bootstrap",
            "target_id": manifest.company.company_id,
            "idempotency_key": _operation_key(
                manifest, "company.bootstrap", manifest.company.company_id
            ),
        }
    ]
    for cell in manifest.cells:
        actions.append(
            {
                "order": len(actions) + 1,
                "tool": "cell.create",
                "target_id": cell.cell_id,
                "idempotency_key": _operation_key(manifest, "cell.create", cell.cell_id),
            }
        )
    for agent in manifest.agents:
        actions.append(
            {
                "order": len(actions) + 1,
                "tool": "agent.register",
                "target_id": agent.agent_instance_id,
                "principal_id": agent.principal_id,
                "definition_id": agent.definition_id,
                "definition_version": agent.definition_version,
                "role": agent.role.value,
                "cell_id": agent.cell_id,
                "idempotency_key": _operation_key(
                    manifest, "agent.register", agent.agent_instance_id
                ),
            }
        )
    return MappingProxyType(
        {
            **dict(manifest.public_summary()),
            "database_role": OPERATOR_DATABASE_ROLE,
            "authority_source": "one-shot-local-founder",
            "identity_activation": "not-performed",
            "actions": actions,
            "postconditions": [
                "company and constitution projections exactly match mutation results",
                "every cell projection and budget exactly match mutation results",
                (
                    "every definition, agent instance, and principal binding "
                    "exactly match mutation results"
                ),
                "every returned domain event and audit record is durable",
            ],
        }
    )


def _one_shot_founder(manifest: BootstrapManifest) -> VerifiedPrincipal:
    credential = secrets.token_urlsafe(32)
    record = PrincipalRecord(
        principal_id=manifest.company.founder_principal_id,
        grants=(
            ScopedRoleGrant(
                Role.FOUNDER,
                ResourceScope.company(manifest.company.company_id),
            ),
        ),
    )
    return LocalTestAuthenticator({credential: record}).authenticate(credential)


def _response_state(response: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    state = response.get("state")
    if not isinstance(state, Mapping):
        raise BootstrapError("service mutation returned an invalid state envelope")
    value = state.get(field_name)
    if not isinstance(value, Mapping):
        raise BootstrapError(f"service mutation did not return {field_name}")
    return value


async def _require_projection(
    transaction: Any,
    projection_type: str,
    projection_id: str,
    expected_state: Mapping[str, object],
) -> None:
    record = await transaction.get_projection(projection_type, projection_id)
    if record is None:
        raise BootstrapError(f"{projection_type} {projection_id} was not durably projected")
    if record.get("revision") != 1:
        raise BootstrapError(f"{projection_type} {projection_id} has an unexpected revision")
    if _plain(record.get("state")) != _plain(expected_state):
        raise BootstrapError(f"{projection_type} {projection_id} differs from mutation result")


async def _require_agent_definition(
    transaction: Any,
    definition_id: str,
    expected_state: Mapping[str, object],
) -> None:
    record = await transaction.get_projection("agent_definition", definition_id)
    if record is None:
        raise BootstrapError(f"agent_definition {definition_id} was not durably projected")
    if record.get("revision") != 1:
        raise BootstrapError(f"agent_definition {definition_id} has an unexpected revision")
    try:
        actual = AgentDefinition.model_validate(record.get("state"))
        expected = AgentDefinition.model_validate(expected_state)
    except PydanticValidationError as error:
        raise BootstrapError(f"agent_definition {definition_id} is invalid") from error
    if actual != expected:
        raise BootstrapError(f"agent_definition {definition_id} differs from manifest")


async def _verify_bootstrap(
    manifest: BootstrapManifest,
    repository: Repository,
    company_response: Mapping[str, object],
    cell_responses: Sequence[Mapping[str, object]],
    agent_responses: Sequence[Mapping[str, object]],
) -> None:
    company_id = manifest.company.company_id
    expected_event_ids: set[str] = set()
    expected_audit_ids: set[str] = set()
    for response in (company_response, *cell_responses, *agent_responses):
        event_ids = response.get("event_ids")
        audit_id = response.get("audit_id")
        if not isinstance(event_ids, list) or not all(isinstance(item, str) for item in event_ids):
            raise BootstrapError("service mutation returned invalid event IDs")
        if not isinstance(audit_id, str):
            raise BootstrapError("service mutation returned an invalid audit ID")
        expected_event_ids.update(event_ids)
        expected_audit_ids.add(audit_id)

    async with repository.transaction(company_id) as transaction:
        await _require_projection(
            transaction,
            "company",
            company_id,
            _response_state(company_response, "company"),
        )
        constitution = _response_state(company_response, "constitution")
        await _require_projection(
            transaction,
            "constitution",
            str(constitution["constitution_id"]),
            constitution,
        )

        company_budget = _response_state(company_response, "budget")
        stored_budget = await transaction.get_budget(str(company_budget["budget_id"]))
        if stored_budget is None or _plain(stored_budget) != _plain(company_budget):
            raise BootstrapError("company budget differs from mutation result")
        company_state = company_response["state"]
        assert isinstance(company_state, Mapping)
        stage_budgets = company_state.get("stage_budgets")
        if not isinstance(stage_budgets, Mapping):
            raise BootstrapError("company bootstrap did not return stage budgets")
        for expected in stage_budgets.values():
            if not isinstance(expected, Mapping):
                raise BootstrapError("company bootstrap returned an invalid stage budget")
            stored = await transaction.get_budget(str(expected["budget_id"]))
            if stored is None or _plain(stored) != _plain(expected):
                raise BootstrapError("stage budget differs from mutation result")

        for cell, response in zip(manifest.cells, cell_responses, strict=True):
            expected_cell = _response_state(response, "cell")
            await _require_projection(transaction, "cell", cell.cell_id, expected_cell)
            expected_budget = _response_state(response, "budget")
            stored = await transaction.get_budget(str(expected_budget["budget_id"]))
            if stored is None or _plain(stored) != _plain(expected_budget):
                raise BootstrapError(f"cell {cell.cell_id} budget differs from mutation result")

        verified_definitions: set[str] = set()
        for agent, response in zip(manifest.agents, agent_responses, strict=True):
            expected_agent = _response_state(response, "agent")
            expected_binding = _response_state(response, "binding")
            await _require_projection(transaction, "agent", agent.agent_instance_id, expected_agent)
            await _require_projection(
                transaction, "agent_binding", agent.principal_id, expected_binding
            )
            if agent.definition_id not in verified_definitions:
                await _require_agent_definition(transaction, agent.definition_id, agent.definition)
                verified_definitions.add(agent.definition_id)

        events = await transaction.list_events(after_position=0, limit=10_000)
        audits = await transaction.list_audit_events(after_position=0, limit=10_000)
        durable_event_ids = {str(event.get("event_id")) for event in events}
        durable_audit_ids = {str(event.get("audit_id")) for event in audits}
        if not expected_event_ids <= durable_event_ids:
            raise BootstrapError("one or more bootstrap domain events are not durable")
        if not expected_audit_ids <= durable_audit_ids:
            raise BootstrapError("one or more bootstrap audit records are not durable")


async def apply_bootstrap(
    manifest: BootstrapManifest,
    repository: Repository,
    *,
    service: SubstrateService | None = None,
) -> Mapping[str, object]:
    """Apply and exactly verify the resumable domain bootstrap mutation sequence."""

    founder = _one_shot_founder(manifest)
    substrate = service or create_service(store=repository)
    company_response = await substrate.invoke(
        "company.bootstrap",
        founder,
        _mutation(
            manifest,
            "company.bootstrap",
            manifest.company.company_id,
            manifest.company_payload,
        ),
    )
    cell_responses: list[Mapping[str, object]] = []
    for cell in manifest.cells:
        cell_responses.append(
            await substrate.invoke(
                "cell.create",
                founder,
                _mutation(manifest, "cell.create", cell.cell_id, cell.payload),
            )
        )
    agent_responses: list[Mapping[str, object]] = []
    for agent in manifest.agents:
        agent_responses.append(
            await substrate.invoke(
                "agent.register",
                founder,
                _mutation(
                    manifest,
                    "agent.register",
                    agent.agent_instance_id,
                    agent.payload,
                ),
            )
        )
    await _verify_bootstrap(
        manifest,
        repository,
        company_response,
        cell_responses,
        agent_responses,
    )
    return MappingProxyType(
        {
            **dict(manifest.public_summary()),
            "status": "verified",
            "company_replayed": bool(company_response.get("replayed")),
            "replayed_cells": sum(bool(response.get("replayed")) for response in cell_responses),
            "replayed_agents": sum(bool(response.get("replayed")) for response in agent_responses),
            "identity_activation": "ready-not-performed",
        }
    )


def require_operator_role(row: Sequence[object] | None) -> None:
    """Require the exact non-privileged offline operator database role."""

    if row is None or len(row) != 9:
        raise BootstrapError("could not verify the database operator role")
    role_name, can_login, *remaining = row
    *unsafe_flags, has_memberships = remaining
    if role_name != OPERATOR_DATABASE_ROLE:
        raise BootstrapError(f"database current_user must be exactly {OPERATOR_DATABASE_ROLE}")
    if can_login is not True:
        raise BootstrapError(f"{OPERATOR_DATABASE_ROLE} must be a LOGIN role")
    if any(flag is not False for flag in unsafe_flags):
        raise BootstrapError(
            f"{OPERATOR_DATABASE_ROLE} must have no privileged or inherited role flags"
        )
    if has_memberships is not False:
        raise BootstrapError(f"{OPERATOR_DATABASE_ROLE} must not be a member of another role")


async def verify_operator_database_role(database_url: str) -> None:
    """Read only pg_catalog before allowing the operator DSN to mutate state."""

    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - project dependency contract
        raise BootstrapError("psycopg is required for closed-beta bootstrap") from error
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        cursor = await connection.execute(
            """
            SELECT current_user,
                   role.rolcanlogin,
                   role.rolsuper,
                   role.rolbypassrls,
                   role.rolcreatedb,
                   role.rolcreaterole,
                   role.rolreplication,
                   role.rolinherit,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.pg_auth_members AS membership
                       WHERE membership.member = role.oid
                   ) AS has_memberships
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = current_user
            """
        )
        require_operator_role(await cursor.fetchone())


def database_url_from_environment(environment: Mapping[str, str] | None = None) -> str:
    """Load the shared operator DSN without accepting it as a CLI argument."""

    source = os.environ if environment is None else environment
    direct = source.get(DATABASE_URL_ENV)
    file_name = source.get(DATABASE_URL_FILE_ENV)
    if bool(direct) == bool(file_name):
        raise BootstrapError(f"set exactly one of {DATABASE_URL_ENV} or {DATABASE_URL_FILE_ENV}")
    if direct is not None:
        if not direct or direct != direct.strip() or "\x00" in direct:
            raise BootstrapError(f"{DATABASE_URL_ENV} is invalid")
        try:
            return require_tls_database_url(direct, field_name=DATABASE_URL_ENV)
        except ValueError as error:
            raise BootstrapError(str(error)) from error

    assert file_name is not None
    path = Path(file_name)
    if path.is_symlink():
        raise BootstrapError(f"{DATABASE_URL_FILE_ENV} must not identify a symlink")
    try:
        metadata = path.stat()
    except OSError as error:
        raise BootstrapError(f"could not stat {DATABASE_URL_FILE_ENV}") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise BootstrapError(f"{DATABASE_URL_FILE_ENV} must identify a regular 0600 file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise BootstrapError(f"{DATABASE_URL_FILE_ENV} file must be owned by the current user")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BootstrapError(f"could not read {DATABASE_URL_FILE_ENV}") from error
    database_url = content.removesuffix("\n")
    if not database_url or database_url != database_url.strip() or "\n" in database_url:
        raise BootstrapError(f"{DATABASE_URL_FILE_ENV} file must contain exactly one DSN line")
    try:
        return require_tls_database_url(database_url, field_name=DATABASE_URL_FILE_ENV)
    except ValueError as error:
        raise BootstrapError(str(error)) from error


def _duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError("manifest contains a duplicate JSON key")
        result[key] = value
    return result


def load_manifest(location: str) -> BootstrapManifest:
    """Read one bounded JSON manifest from a file or standard input."""

    try:
        if location == "-":
            encoded = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        else:
            with Path(location).open("rb") as stream:
                encoded = stream.read(MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        raise BootstrapError("could not read bootstrap manifest") from error
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise BootstrapError(f"bootstrap manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        parsed = json.loads(encoded, object_pairs_hook=_duplicate_safe_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError("bootstrap manifest must be valid UTF-8 JSON") from error
    return BootstrapManifest.from_mapping(_mapping(parsed, "manifest"))


async def _apply_production(manifest: BootstrapManifest, database_url: str) -> Mapping[str, object]:
    await verify_operator_database_role(database_url)
    repository = PostgresRepository(database_url, min_size=1, max_size=1)
    try:
        return await apply_bootstrap(manifest, repository)
    finally:
        await repository.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply canonical closed-beta domain bootstrap",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--manifest",
            required=True,
            help="versioned non-secret JSON manifest path, or - for stdin",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "plan":
            result = bootstrap_plan(manifest)
        else:
            result = asyncio.run(_apply_production(manifest, database_url_from_environment()))
    except BootstrapError as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 2
    except Exception as error:
        # Service/database failures are content-minimized; credentials and payloads
        # never appear in the operator-facing error.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": f"bootstrap failed: {type(error).__name__}",
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(_plain(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main tests
    raise SystemExit(main())

#!/usr/bin/env python3
"""Provision one closed-beta OAuth subject, agent instance, and active run.

This is an operator tool, not part of the hosted runtime.  It deliberately accepts
the database credential only through the environment (or a permission-restricted
file named by the environment), executes parameterized SQL, and fails when an
existing record differs from the requested immutable binding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from ai_nature_company.database_url import require_tls_database_url
from ai_nature_company.mcp_resource import is_mcp_resource_path_slug

MAX_IDENTIFIER_LENGTH = 256
MAX_JSON_BYTES = 1_048_576
DATABASE_URL_ENV = "ANC_OPERATOR_DATABASE_URL"
DATABASE_URL_FILE_ENV = "ANC_OPERATOR_DATABASE_URL_FILE"
RUNTIME_DATABASE_ROLE = "ai_nature_company_runtime"
OPERATOR_DATABASE_ROLE = "ai_nature_company_operator"
ROLES = frozenset({"cell_owner", "specialist", "evaluator", "allocator", "immune", "observer"})
CELL_REQUIRED_ROLES = frozenset({"cell_owner", "specialist", "evaluator"})
ROTATION_KINDS = frozenset({"expiry", "revocation_replacement"})
RUN_EXPIRED_TERMINATION = "run_expired"


class ProvisioningError(ValueError):
    """The requested binding is unsafe, invalid, or conflicts with stored state."""


class Cursor(Protocol):
    def fetchone(self) -> Sequence[Any] | None:
        """Return one database row."""


class Transaction(Protocol):
    def __enter__(self) -> object:
        """Enter a database transaction."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        """Commit or roll back the transaction."""


class Connection(Protocol):
    def execute(self, query: str, params: Mapping[str, object] | None = None) -> Cursor:
        """Execute one parameterized query."""

    def transaction(self) -> Transaction:
        """Create an explicit transaction context."""


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProvisioningError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ProvisioningError(f"{field_name} must be a non-empty canonical identifier")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ProvisioningError(f"{field_name} exceeds {MAX_IDENTIFIER_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProvisioningError(f"{field_name} cannot contain control characters")
    return value


def _uuid(value: object, field_name: str) -> str:
    canonical = _identifier(value, field_name)
    try:
        parsed = UUID(canonical)
    except (AttributeError, TypeError, ValueError) as error:
        raise ProvisioningError(f"{field_name} must be a canonical UUID") from error
    if str(parsed) != canonical:
        raise ProvisioningError(f"{field_name} must be a canonical UUID")
    return canonical


def _https_url(value: object, field_name: str, *, required_path_suffix: str) -> str:
    canonical = _identifier(value, field_name)
    parsed = urlsplit(canonical)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisioningError(f"{field_name} must be a canonical HTTPS URL")
    if not parsed.path.endswith(required_path_suffix) or parsed.path.endswith("/"):
        raise ProvisioningError(f"{field_name} must end with {required_path_suffix}")
    return canonical


def _mcp_resource_url(value: object, field_name: str) -> str:
    canonical = _identifier(value, field_name)
    parsed = urlsplit(canonical)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisioningError(f"{field_name} must be a canonical HTTPS URL")
    if parsed.path == "/mcp":
        return canonical
    if parsed.path.startswith("/mcp/"):
        slug = parsed.path[len("/mcp/") :]
        if is_mcp_resource_path_slug(slug):
            return canonical
        raise ProvisioningError(f"{field_name} must contain a canonical MCP path")
    raise ProvisioningError(f"{field_name} must end with /mcp")


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProvisioningError(f"{field_name} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ProvisioningError(f"{field_name} must be an RFC 3339 UTC timestamp") from error
    if parsed.tzinfo is None:
        raise ProvisioningError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _auth_subject_id(*, issuer: str, subject: str, client_id: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            [issuer, subject, client_id],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"authsub_{digest}"


@dataclass(frozen=True, slots=True)
class ProvisioningSpec:
    """Validated, non-secret desired state for one hosted agent run."""

    company_id: str
    oauth_client_id: str
    oauth_client_display_name: str
    resource_url: str
    issuer: str
    external_subject: str
    principal_id: str
    agent_instance_id: str
    definition_id: str
    definition_version: int
    role: str
    cell_id: str | None
    agent_run_id: str
    provisioned_at: datetime
    run_started_at: datetime
    run_expires_at: datetime
    auth_subject_id: str
    run_created_at: datetime | None = None
    rotation_kind: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        definition: Mapping[str, object],
        now: datetime | None = None,
        rotate: bool = False,
    ) -> ProvisioningSpec:
        expected_fields = {
            "company_id",
            "oauth_client_id",
            "oauth_client_display_name",
            "resource_url",
            "issuer",
            "external_subject",
            "principal_id",
            "agent_instance_id",
            "definition_id",
            "definition_version",
            "role",
            "cell_id",
            "agent_run_id",
            "provisioned_at",
            "run_started_at",
            "run_expires_at",
        }
        rotation_fields = {"run_created_at", "rotation_kind"}
        allowed_fields = expected_fields | (rotation_fields if rotate else set())
        missing = sorted(expected_fields - value.keys())
        unknown = sorted(value.keys() - allowed_fields)
        if rotate:
            missing.extend(sorted(rotation_fields - value.keys()))
        if missing:
            raise ProvisioningError(f"manifest is missing fields: {', '.join(missing)}")
        if unknown:
            raise ProvisioningError(f"manifest contains unknown fields: {', '.join(unknown)}")

        company_id = _uuid(value["company_id"], "company_id")
        oauth_client_id = _uuid(value["oauth_client_id"], "oauth_client_id")
        display_name = _identifier(
            value["oauth_client_display_name"],
            "oauth_client_display_name",
        )
        resource_url = _mcp_resource_url(value["resource_url"], "resource_url")
        issuer = _https_url(value["issuer"], "issuer", required_path_suffix="/auth/v1")
        external_subject = _uuid(value["external_subject"], "external_subject")
        principal_id = _uuid(value["principal_id"], "principal_id")
        agent_instance_id = _uuid(value["agent_instance_id"], "agent_instance_id")
        definition_id = _uuid(value["definition_id"], "definition_id")
        definition_version = value["definition_version"]
        if (
            isinstance(definition_version, bool)
            or not isinstance(definition_version, int)
            or definition_version < 1
        ):
            raise ProvisioningError("definition_version must be a positive integer")
        role = _identifier(value["role"], "role")
        if role not in ROLES:
            raise ProvisioningError("role is not an agent role")
        raw_cell_id = value["cell_id"]
        cell_id = _uuid(raw_cell_id, "cell_id") if raw_cell_id is not None else None
        if role in CELL_REQUIRED_ROLES and cell_id is None:
            raise ProvisioningError(f"cell_id is required for the {role} closed-beta role")
        agent_run_id = _uuid(value["agent_run_id"], "agent_run_id")
        provisioned_at = _timestamp(value["provisioned_at"], "provisioned_at")
        run_started_at = _timestamp(value["run_started_at"], "run_started_at")
        run_expires_at = _timestamp(value["run_expires_at"], "run_expires_at")
        effective_now = (now or datetime.now(UTC)).astimezone(UTC)
        if provisioned_at > run_started_at:
            raise ProvisioningError("provisioned_at cannot be later than run_started_at")
        if run_started_at > effective_now:
            raise ProvisioningError("run_started_at cannot be in the future")
        if run_expires_at <= effective_now or run_expires_at <= run_started_at:
            raise ProvisioningError("run_expires_at must be later than now and run_started_at")
        run_created_at = None
        rotation_kind = None
        if rotate:
            run_created_at = _timestamp(value["run_created_at"], "run_created_at")
            rotation_kind = _identifier(value["rotation_kind"], "rotation_kind")
            if rotation_kind not in ROTATION_KINDS:
                raise ProvisioningError("rotation_kind must be expiry or revocation_replacement")
            if run_created_at > effective_now:
                raise ProvisioningError("run_created_at cannot be in the future")
            if provisioned_at > run_created_at:
                raise ProvisioningError("provisioned_at cannot be later than run_created_at")
            if run_created_at > run_started_at:
                raise ProvisioningError("run_created_at cannot be later than run_started_at")

        _validate_definition(
            definition,
            definition_id=definition_id,
            definition_version=definition_version,
            role=role,
        )
        return cls(
            company_id=company_id,
            oauth_client_id=oauth_client_id,
            oauth_client_display_name=display_name,
            resource_url=resource_url,
            issuer=issuer,
            external_subject=external_subject,
            principal_id=principal_id,
            agent_instance_id=agent_instance_id,
            definition_id=definition_id,
            definition_version=definition_version,
            role=role,
            cell_id=cell_id,
            agent_run_id=agent_run_id,
            provisioned_at=provisioned_at,
            run_started_at=run_started_at,
            run_expires_at=run_expires_at,
            auth_subject_id=_auth_subject_id(
                issuer=issuer,
                subject=external_subject,
                client_id=oauth_client_id,
            ),
            run_created_at=run_created_at,
            rotation_kind=rotation_kind,
        )

    @property
    def parameters(self) -> Mapping[str, object]:
        values = asdict(self)
        values.update(
            {
                "subject_kind": "agent",
                "active_status": "active",
                "running_status": "running",
                "provisioned_at": self.provisioned_at,
                "run_started_at": self.run_started_at,
                "run_expires_at": self.run_expires_at,
                "run_created_at": self.run_created_at or self.provisioned_at,
                "rotation_kind": self.rotation_kind,
                "expired_status": "expired",
                "expired_termination_code": RUN_EXPIRED_TERMINATION,
                "lock_key": f"anc-provision:{self.company_id}:{self.principal_id}",
            }
        )
        return MappingProxyType(values)

    def public_summary(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "company_id": self.company_id,
                "oauth_client_id": self.oauth_client_id,
                "auth_subject_id": self.auth_subject_id,
                "principal_id": self.principal_id,
                "agent_instance_id": self.agent_instance_id,
                "definition_id": self.definition_id,
                "definition_version": self.definition_version,
                "role": self.role,
                "cell_id": self.cell_id,
                "agent_run_id": self.agent_run_id,
                "run_expires_at": _timestamp_text(self.run_expires_at),
                "rotation_kind": self.rotation_kind,
                "run_created_at": (
                    _timestamp_text(self.run_created_at)
                    if self.run_created_at is not None
                    else None
                ),
            }
        )


def _validate_definition(
    definition: Mapping[str, object],
    *,
    definition_id: str,
    definition_version: int,
    role: str,
) -> None:
    if definition.get("definition_id") != definition_id:
        raise ProvisioningError("definition file does not match definition_id")
    if definition.get("version") != definition_version:
        raise ProvisioningError("definition file does not match definition_version")
    compatible_roles = definition.get("compatible_roles")
    if not isinstance(compatible_roles, list) or not all(
        isinstance(item, str) for item in compatible_roles
    ):
        raise ProvisioningError("definition compatible_roles must be a list of strings")
    if role not in compatible_roles:
        raise ProvisioningError("role is incompatible with the reviewed definition")


@dataclass(frozen=True, slots=True)
class SQLStep:
    name: str
    sql: str
    parameter_names: tuple[str, ...]

    def plan_item(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        rendered_parameters: dict[str, object] = {}
        for name in self.parameter_names:
            value = parameters[name]
            rendered_parameters[name] = (
                _timestamp_text(value) if isinstance(value, datetime) else value
            )
        return {
            "name": self.name,
            "sql": self.sql.strip(),
            "parameters": rendered_parameters,
        }


SAFETY_STEP = SQLStep(
    "verify_operator_role",
    """
    SELECT current_user,
           role.rolsuper,
           role.rolbypassrls,
           role.rolcreatedb,
           role.rolcreaterole,
           role.rolreplication,
           role.rolinherit,
           role.rolcanlogin,
           NOT EXISTS (
               SELECT 1
               FROM pg_catalog.pg_auth_members AS membership
               WHERE membership.member = role.oid
           ) AS has_no_role_memberships
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_user
    """,
    (),
)
MIGRATION_STEP = SQLStep(
    "verify_identity_migration",
    """
    SELECT to_regclass('public.anc_oauth_clients')::text,
           to_regclass('public.anc_auth_subjects')::text,
           to_regclass('public.anc_agent_instances')::text,
           to_regclass('public.anc_agent_runs')::text,
           to_regclass('public.anc_oauth_client_resource_aliases')::text
    """,
    (),
)
LOCK_STEP = SQLStep(
    "lock_principal_bundle",
    "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
    ("lock_key",),
)
SCOPE_STEP = SQLStep(
    "set_company_scope",
    "SELECT set_config('app.company_id', %(company_id)s, true)",
    ("company_id",),
)
SELECT_DOMAIN_AUTHORITY = SQLStep(
    "verify_domain_authority",
    """
    SELECT
        (SELECT state
         FROM public.anc_projections
         WHERE company_id = %(company_id)s
           AND projection_type = 'agent_definition'
           AND projection_id = %(definition_id)s),
        (SELECT state
         FROM public.anc_projections
         WHERE company_id = %(company_id)s
           AND projection_type = 'agent'
           AND projection_id = %(agent_instance_id)s),
        (SELECT state
         FROM public.anc_projections
         WHERE company_id = %(company_id)s
           AND projection_type = 'agent_binding'
           AND projection_id = %(principal_id)s),
        EXISTS (
            SELECT 1 FROM public.anc_projections
            WHERE company_id = %(company_id)s
              AND projection_type = 'company'
              AND projection_id = %(company_id)s
        ),
        (CAST(%(cell_id)s AS text) IS NULL OR EXISTS (
            SELECT 1 FROM public.anc_projections
            WHERE company_id = %(company_id)s
              AND projection_type = 'cell'
              AND projection_id = %(cell_id)s
              AND COALESCE(state->>'state', '') <> 'killed'
        ))
    """,
    (
        "company_id",
        "definition_id",
        "agent_instance_id",
        "principal_id",
        "cell_id",
    ),
)

INSERT_CLIENT = SQLStep(
    "insert_oauth_client",
    """
    INSERT INTO public.anc_oauth_clients (
        client_id, company_id, resource_url, display_name, status,
        revision, created_at, updated_at
    ) VALUES (
        %(oauth_client_id)s, %(company_id)s, %(resource_url)s,
        %(oauth_client_display_name)s, %(active_status)s,
        0, %(provisioned_at)s, %(provisioned_at)s
    )
    ON CONFLICT DO NOTHING
    """,
    (
        "oauth_client_id",
        "company_id",
        "resource_url",
        "oauth_client_display_name",
        "active_status",
        "provisioned_at",
    ),
)
SELECT_CLIENT = SQLStep(
    "verify_oauth_client",
    """
    SELECT company_id, client_id, resource_url, display_name, status
    FROM public.anc_oauth_clients
    WHERE company_id = %(company_id)s AND client_id = %(oauth_client_id)s
    FOR UPDATE
    """,
    ("company_id", "oauth_client_id"),
)
INSERT_ALIAS = SQLStep(
    "insert_oauth_client_resource_alias",
    """
    INSERT INTO public.anc_oauth_client_resource_aliases (
        company_id, client_id, resource_url, status, revision, created_at, updated_at
    ) VALUES (
        %(company_id)s, %(oauth_client_id)s, %(resource_url)s,
        %(active_status)s, 0, %(provisioned_at)s, %(provisioned_at)s
    )
    ON CONFLICT DO NOTHING
    """,
    (
        "company_id",
        "oauth_client_id",
        "resource_url",
        "active_status",
        "provisioned_at",
    ),
)
SELECT_ALIAS = SQLStep(
    "verify_oauth_client_resource_alias",
    """
    SELECT company_id, client_id, resource_url, status
    FROM public.anc_oauth_client_resource_aliases
    WHERE company_id = %(company_id)s
      AND client_id = %(oauth_client_id)s
      AND resource_url = %(resource_url)s
    FOR UPDATE
    """,
    ("company_id", "oauth_client_id", "resource_url"),
)

INSERT_SUBJECT = SQLStep(
    "insert_auth_subject",
    """
    INSERT INTO public.anc_auth_subjects (
        company_id, auth_subject_id, issuer, external_subject, oauth_client_id,
        principal_id, subject_kind, status, token_valid_after, disabled_at,
        revision, created_at, updated_at
    ) VALUES (
        %(company_id)s, %(auth_subject_id)s, %(issuer)s, %(external_subject)s,
        %(oauth_client_id)s, %(principal_id)s, %(subject_kind)s,
        %(active_status)s, %(provisioned_at)s, NULL, 0,
        %(provisioned_at)s, %(provisioned_at)s
    )
    ON CONFLICT DO NOTHING
    """,
    (
        "company_id",
        "auth_subject_id",
        "issuer",
        "external_subject",
        "oauth_client_id",
        "principal_id",
        "subject_kind",
        "active_status",
        "provisioned_at",
    ),
)
SELECT_SUBJECT = SQLStep(
    "verify_auth_subject",
    """
    SELECT company_id, auth_subject_id, issuer, external_subject,
           oauth_client_id, principal_id, subject_kind, status,
           token_valid_after, disabled_at
    FROM public.anc_auth_subjects
    WHERE company_id = %(company_id)s AND auth_subject_id = %(auth_subject_id)s
    FOR UPDATE
    """,
    ("company_id", "auth_subject_id"),
)

INSERT_INSTANCE = SQLStep(
    "insert_agent_instance",
    """
    INSERT INTO public.anc_agent_instances (
        company_id, agent_instance_id, principal_id, definition_id,
        definition_version, role, cell_id, status, revision, created_at, updated_at
    ) VALUES (
        %(company_id)s, %(agent_instance_id)s, %(principal_id)s,
        %(definition_id)s, %(definition_version)s, %(role)s, %(cell_id)s,
        %(active_status)s, 0, %(provisioned_at)s, %(provisioned_at)s
    )
    ON CONFLICT DO NOTHING
    """,
    (
        "company_id",
        "agent_instance_id",
        "principal_id",
        "definition_id",
        "definition_version",
        "role",
        "cell_id",
        "active_status",
        "provisioned_at",
    ),
)
SELECT_INSTANCE = SQLStep(
    "verify_agent_instance",
    """
    SELECT company_id, agent_instance_id, principal_id, definition_id,
           definition_version, role, cell_id, status
    FROM public.anc_agent_instances
    WHERE company_id = %(company_id)s
      AND agent_instance_id = %(agent_instance_id)s
    FOR UPDATE
    """,
    ("company_id", "agent_instance_id"),
)

INSERT_RUN = SQLStep(
    "insert_agent_run",
    """
    INSERT INTO public.anc_agent_runs (
        company_id, agent_run_id, agent_instance_id, principal_id,
        definition_id, definition_version, role, cell_id, status,
        termination_code, revision, started_at, expires_at, ended_at,
        created_at, updated_at
    ) VALUES (
        %(company_id)s, %(agent_run_id)s, %(agent_instance_id)s,
        %(principal_id)s, %(definition_id)s, %(definition_version)s,
        %(role)s, %(cell_id)s, %(running_status)s, NULL, 0,
        %(run_started_at)s, %(run_expires_at)s, NULL,
        %(provisioned_at)s, %(provisioned_at)s
    )
    ON CONFLICT DO NOTHING
    """,
    (
        "company_id",
        "agent_run_id",
        "agent_instance_id",
        "principal_id",
        "definition_id",
        "definition_version",
        "role",
        "cell_id",
        "running_status",
        "run_started_at",
        "run_expires_at",
        "provisioned_at",
    ),
)
SELECT_RUN = SQLStep(
    "verify_agent_run",
    """
    SELECT company_id, agent_run_id, agent_instance_id, principal_id,
           definition_id, definition_version, role, cell_id, status,
           started_at, expires_at, ended_at, termination_code
    FROM public.anc_agent_runs
    WHERE company_id = %(company_id)s AND agent_run_id = %(agent_run_id)s
    FOR UPDATE
    """,
    ("company_id", "agent_run_id"),
)
SELECT_SUBJECT_BINDING = SQLStep(
    "verify_auth_subject_binding",
    """
    SELECT company_id, auth_subject_id, issuer, external_subject,
           oauth_client_id, principal_id, subject_kind, status,
           token_valid_after, disabled_at, created_at
    FROM public.anc_auth_subjects
    WHERE company_id = %(company_id)s AND auth_subject_id = %(auth_subject_id)s
    FOR UPDATE
    """,
    ("company_id", "auth_subject_id"),
)
SELECT_RUNNING_RUN = SQLStep(
    "lock_running_run",
    """
    SELECT company_id, agent_run_id, agent_instance_id, principal_id,
           definition_id, definition_version, role, cell_id, status,
           started_at, expires_at, ended_at, termination_code, revision
    FROM public.anc_agent_runs
    WHERE company_id = %(company_id)s
      AND agent_instance_id = %(agent_instance_id)s
      AND status = %(running_status)s
    FOR UPDATE
    """,
    ("company_id", "agent_instance_id", "running_status"),
)
EXPIRE_RUNNING_RUN = SQLStep(
    "expire_predecessor_run",
    """
    UPDATE public.anc_agent_runs
    SET status = %(expired_status)s,
        ended_at = %(run_created_at)s,
        termination_code = %(expired_termination_code)s,
        revision = %(predecessor_revision)s + 1,
        updated_at = %(run_created_at)s
    WHERE company_id = %(company_id)s
      AND agent_run_id = %(predecessor_run_id)s
      AND agent_instance_id = %(agent_instance_id)s
      AND status = %(running_status)s
      AND expires_at <= %(effective_now)s
      AND revision = %(predecessor_revision)s
    """,
    (
        "expired_status",
        "run_created_at",
        "expired_termination_code",
        "predecessor_revision",
        "company_id",
        "predecessor_run_id",
        "agent_instance_id",
        "running_status",
        "effective_now",
    ),
)
INSERT_SUCCESSOR_RUN = SQLStep(
    "insert_successor_run",
    """
    INSERT INTO public.anc_agent_runs (
        company_id, agent_run_id, agent_instance_id, principal_id,
        definition_id, definition_version, role, cell_id, status,
        termination_code, revision, started_at, expires_at, ended_at,
        created_at, updated_at
    ) VALUES (
        %(company_id)s, %(agent_run_id)s, %(agent_instance_id)s,
        %(principal_id)s, %(definition_id)s, %(definition_version)s,
        %(role)s, %(cell_id)s, %(running_status)s, NULL, 0,
        %(run_started_at)s, %(run_expires_at)s, NULL,
        %(run_created_at)s, %(run_created_at)s
    )
    ON CONFLICT DO NOTHING
    """,
    (
        "company_id",
        "agent_run_id",
        "agent_instance_id",
        "principal_id",
        "definition_id",
        "definition_version",
        "role",
        "cell_id",
        "running_status",
        "run_started_at",
        "run_expires_at",
        "run_created_at",
    ),
)

SQL_STEPS = (
    SAFETY_STEP,
    MIGRATION_STEP,
    LOCK_STEP,
    SCOPE_STEP,
    SELECT_DOMAIN_AUTHORITY,
    INSERT_CLIENT,
    SELECT_CLIENT,
    INSERT_ALIAS,
    SELECT_ALIAS,
    INSERT_SUBJECT,
    SELECT_SUBJECT,
    INSERT_INSTANCE,
    SELECT_INSTANCE,
    INSERT_RUN,
    SELECT_RUN,
)
ROTATION_SQL_STEPS = (
    SAFETY_STEP,
    MIGRATION_STEP,
    LOCK_STEP,
    SCOPE_STEP,
    SELECT_DOMAIN_AUTHORITY,
    SELECT_CLIENT,
    SELECT_ALIAS,
    SELECT_SUBJECT_BINDING,
    SELECT_INSTANCE,
    SELECT_RUNNING_RUN,
    EXPIRE_RUNNING_RUN,
    INSERT_SUCCESSOR_RUN,
    SELECT_RUN,
)


def parameterized_plan(
    spec: ProvisioningSpec,
    *,
    rotate: bool = False,
) -> Mapping[str, object]:
    """Return auditable SQL and separate non-secret parameters; never a DSN."""

    parameters = dict(spec.parameters)
    if rotate:
        if spec.rotation_kind is None or spec.run_created_at is None:
            raise ProvisioningError("rotation requires run_created_at and rotation_kind")
        parameters["effective_now"] = spec.run_created_at
        parameters["predecessor_run_id"] = "<locked-running-run>"
        parameters["predecessor_revision"] = "<locked-running-revision>"
    return {
        "transaction": "required",
        "exact_idempotency": True,
        "rotation": rotate,
        "rotation_kind": spec.rotation_kind,
        "session_behavior": (
            "preserve_post_cutoff_session"
            if spec.rotation_kind == "expiry"
            else "require_fresh_post_cutoff_authorization"
            if spec.rotation_kind == "revocation_replacement"
            else "initial_activation"
        ),
        "steps": [
            step.plan_item(parameters) for step in (ROTATION_SQL_STEPS if rotate else SQL_STEPS)
        ],
    }


def _normalize_row_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ProvisioningError("database returned a naive timestamp")
        return value.astimezone(UTC)
    return value


def _require_listed_oauth_resource(
    connection: Connection,
    spec: ProvisioningSpec,
    parameters: Mapping[str, object],
    *,
    attach: bool,
) -> None:
    """Accept the client's primary resource_url or an active alias of that client."""

    client_row = connection.execute(SELECT_CLIENT.sql, parameters).fetchone()
    if client_row is None:
        raise ProvisioningError(
            "OAuth client was not created; a unique binding may already use this identity"
        )
    actual_client = tuple(_normalize_row_value(value) for value in client_row)
    expected_client = (
        spec.company_id,
        spec.oauth_client_id,
        spec.oauth_client_display_name,
        "active",
    )
    if (
        len(actual_client) != 5
        or (actual_client[0], actual_client[1], actual_client[3], actual_client[4])
        != expected_client
    ):
        raise ProvisioningError("existing OAuth client differs from the requested binding")
    primary_resource_url = actual_client[2]
    if attach:
        connection.execute(INSERT_ALIAS.sql, parameters)
        _require_exact_row(
            connection.execute(SELECT_ALIAS.sql, parameters),
            record_name="OAuth client resource",
            expected=(
                spec.company_id,
                spec.oauth_client_id,
                spec.resource_url,
                "active",
            ),
        )
        return
    if primary_resource_url == spec.resource_url:
        return
    alias_row = connection.execute(SELECT_ALIAS.sql, parameters).fetchone()
    actual_alias = (
        None if alias_row is None else tuple(_normalize_row_value(value) for value in alias_row)
    )
    if actual_alias != (
        spec.company_id,
        spec.oauth_client_id,
        spec.resource_url,
        "active",
    ):
        raise ProvisioningError("OAuth client does not list this protected resource")


def _require_exact_row(
    cursor: Cursor,
    *,
    record_name: str,
    expected: Sequence[object],
) -> None:
    row = cursor.fetchone()
    if row is None:
        raise ProvisioningError(
            f"{record_name} was not created; a unique binding may already use this identity"
        )
    actual = tuple(_normalize_row_value(value) for value in row)
    normalized_expected = tuple(_normalize_row_value(value) for value in expected)
    if actual != normalized_expected:
        raise ProvisioningError(f"existing {record_name} differs from the requested binding")


def _require_domain_authority(cursor: Cursor, spec: ProvisioningSpec) -> None:
    row = cursor.fetchone()
    if row is None or len(row) != 5:
        raise ProvisioningError("could not verify the registered domain authority")
    definition, instance, binding, company_exists, cell_exists = row
    if company_exists is not True:
        raise ProvisioningError("company must be bootstrapped before identity provisioning")
    if cell_exists is not True:
        raise ProvisioningError(
            "bound cell must exist and not be killed before identity provisioning"
        )
    for value, label in (
        (definition, "agent definition"),
        (instance, "agent instance"),
        (binding, "agent binding"),
    ):
        if not isinstance(value, Mapping):
            raise ProvisioningError(f"registered {label} projection is missing")

    compatible_roles = definition.get("compatible_roles")
    expected_definition = {
        "definition_id": spec.definition_id,
        "version": spec.definition_version,
    }
    actual_definition = {
        "definition_id": definition.get("definition_id"),
        "version": definition.get("version"),
    }
    if actual_definition != expected_definition or not isinstance(compatible_roles, list):
        raise ProvisioningError("registered agent definition differs from the manifest")
    if spec.role not in compatible_roles:
        raise ProvisioningError("registered agent definition is incompatible with the role")

    expected_instance = {
        "agent_instance_id": spec.agent_instance_id,
        "principal_id": spec.principal_id,
        "definition_id": spec.definition_id,
        "definition_version": spec.definition_version,
        "role": spec.role,
        "cell_id": spec.cell_id,
        "status": "active",
    }
    actual_instance = {
        key: instance.get(key)
        for key in (
            "agent_instance_id",
            "principal_id",
            "definition_id",
            "definition_version",
            "role",
            "cell_id",
            "status",
        )
    }
    if actual_instance != expected_instance:
        raise ProvisioningError("registered agent instance differs from the manifest")

    expected_binding = {
        "principal_id": spec.principal_id,
        "agent_instance_id": spec.agent_instance_id,
        "definition_id": spec.definition_id,
        "definition_version": spec.definition_version,
        "role": spec.role,
        "cell_id": spec.cell_id,
        "status": "active",
    }
    actual_binding = {
        key: binding.get(key)
        for key in (
            "principal_id",
            "agent_instance_id",
            "definition_id",
            "definition_version",
            "role",
            "cell_id",
            "status",
        )
    }
    if actual_binding != expected_binding:
        raise ProvisioningError("registered agent binding differs from the manifest")


def apply_provisioning(spec: ProvisioningSpec, connection: Connection) -> Mapping[str, object]:
    """Apply an exact-idempotent bundle in one database transaction."""

    parameters = spec.parameters
    with connection.transaction():
        role_row = connection.execute(SAFETY_STEP.sql).fetchone()
        if role_row is None or len(role_row) != 9:
            raise ProvisioningError("could not verify the database operator role")
        if role_row[0] != OPERATOR_DATABASE_ROLE:
            raise ProvisioningError(f"database current_user must be {OPERATOR_DATABASE_ROLE}")
        if (
            any(flag is not False for flag in role_row[1:7])
            or role_row[7] is not True
            or role_row[8] is not True
        ):
            raise ProvisioningError("the database operator role has unsafe attributes")

        migration_row = connection.execute(MIGRATION_STEP.sql).fetchone()
        if (
            migration_row is None
            or len(migration_row) != 5
            or any(table is None for table in migration_row)
        ):
            raise ProvisioningError("agent identity migration has not been applied")

        connection.execute(LOCK_STEP.sql, parameters)
        connection.execute(SCOPE_STEP.sql, parameters)
        _require_domain_authority(
            connection.execute(SELECT_DOMAIN_AUTHORITY.sql, parameters),
            spec,
        )

        connection.execute(INSERT_CLIENT.sql, parameters)
        _require_listed_oauth_resource(connection, spec, parameters, attach=True)

        connection.execute(INSERT_SUBJECT.sql, parameters)
        _require_exact_row(
            connection.execute(SELECT_SUBJECT.sql, parameters),
            record_name="authentication subject",
            expected=(
                spec.company_id,
                spec.auth_subject_id,
                spec.issuer,
                spec.external_subject,
                spec.oauth_client_id,
                spec.principal_id,
                "agent",
                "active",
                spec.provisioned_at,
                None,
            ),
        )

        connection.execute(INSERT_INSTANCE.sql, parameters)
        _require_exact_row(
            connection.execute(SELECT_INSTANCE.sql, parameters),
            record_name="agent instance",
            expected=(
                spec.company_id,
                spec.agent_instance_id,
                spec.principal_id,
                spec.definition_id,
                spec.definition_version,
                spec.role,
                spec.cell_id,
                "active",
            ),
        )

        connection.execute(INSERT_RUN.sql, parameters)
        _require_exact_row(
            connection.execute(SELECT_RUN.sql, parameters),
            record_name="agent run",
            expected=(
                spec.company_id,
                spec.agent_run_id,
                spec.agent_instance_id,
                spec.principal_id,
                spec.definition_id,
                spec.definition_version,
                spec.role,
                spec.cell_id,
                "running",
                spec.run_started_at,
                spec.run_expires_at,
                None,
                None,
            ),
        )
    return spec.public_summary()


def _verify_operator_and_scope(spec: ProvisioningSpec, connection: Connection) -> None:
    parameters = spec.parameters
    role_row = connection.execute(SAFETY_STEP.sql).fetchone()
    if role_row is None or len(role_row) != 9:
        raise ProvisioningError("could not verify the database operator role")
    if role_row[0] != OPERATOR_DATABASE_ROLE:
        raise ProvisioningError(f"database current_user must be {OPERATOR_DATABASE_ROLE}")
    if (
        any(flag is not False for flag in role_row[1:7])
        or role_row[7] is not True
        or role_row[8] is not True
    ):
        raise ProvisioningError("the database operator role has unsafe attributes")

    migration_row = connection.execute(MIGRATION_STEP.sql).fetchone()
    if (
        migration_row is None
        or len(migration_row) != 5
        or any(table is None for table in migration_row)
    ):
        raise ProvisioningError("agent identity migration has not been applied")

    connection.execute(LOCK_STEP.sql, parameters)
    connection.execute(SCOPE_STEP.sql, parameters)
    _require_domain_authority(
        connection.execute(SELECT_DOMAIN_AUTHORITY.sql, parameters),
        spec,
    )


def _running_run_expected(spec: ProvisioningSpec) -> tuple[object, ...]:
    return (
        spec.company_id,
        spec.agent_run_id,
        spec.agent_instance_id,
        spec.principal_id,
        spec.definition_id,
        spec.definition_version,
        spec.role,
        spec.cell_id,
        "running",
        spec.run_started_at,
        spec.run_expires_at,
        None,
        None,
    )


def apply_rotation(
    spec: ProvisioningSpec,
    connection: Connection,
    *,
    now: datetime | None = None,
) -> Mapping[str, object]:
    """Retire an expired running run and insert its successor in one transaction."""

    if spec.rotation_kind not in ROTATION_KINDS or spec.run_created_at is None:
        raise ProvisioningError("rotation requires run_created_at and rotation_kind")
    effective_now = (now or datetime.now(UTC)).astimezone(UTC)
    parameters = dict(spec.parameters)
    parameters["effective_now"] = effective_now
    with connection.transaction():
        _verify_operator_and_scope(spec, connection)
        _require_listed_oauth_resource(connection, spec, parameters, attach=False)
        subject_row = connection.execute(SELECT_SUBJECT_BINDING.sql, parameters).fetchone()
        if subject_row is None:
            raise ProvisioningError(
                "authentication subject was not created; "
                "a unique binding may already use this identity"
            )
        actual_subject = tuple(_normalize_row_value(value) for value in subject_row)
        expected_identity = (
            spec.company_id,
            spec.auth_subject_id,
            spec.issuer,
            spec.external_subject,
            spec.oauth_client_id,
            spec.principal_id,
            "agent",
            "active",
        )
        if actual_subject[:8] != expected_identity or actual_subject[9] is not None:
            raise ProvisioningError(
                "existing authentication subject differs from the requested binding"
            )
        token_valid_after = actual_subject[8]
        created_at = actual_subject[10]
        if created_at != spec.provisioned_at:
            raise ProvisioningError(
                "subject binding created_at must remain the original provisioned_at"
            )
        if spec.rotation_kind == "expiry" and token_valid_after != spec.provisioned_at:
            raise ProvisioningError("expiry rotation must not change token_valid_after")
        if spec.rotation_kind == "revocation_replacement":
            if not isinstance(token_valid_after, datetime):
                raise ProvisioningError("revocation replacement requires an advanced token cutoff")
            if token_valid_after < spec.provisioned_at:
                raise ProvisioningError("token_valid_after cannot move backward")

        _require_exact_row(
            connection.execute(SELECT_INSTANCE.sql, parameters),
            record_name="agent instance",
            expected=(
                spec.company_id,
                spec.agent_instance_id,
                spec.principal_id,
                spec.definition_id,
                spec.definition_version,
                spec.role,
                spec.cell_id,
                "active",
            ),
        )

        running = connection.execute(SELECT_RUNNING_RUN.sql, parameters).fetchone()
        if running is not None:
            running_values = tuple(_normalize_row_value(value) for value in running)
            predecessor_id = running_values[1]
            predecessor_expires = running_values[10]
            predecessor_revision = running_values[13]
            if predecessor_id == spec.agent_run_id:
                _require_exact_row(
                    connection.execute(SELECT_RUN.sql, parameters),
                    record_name="agent run",
                    expected=_running_run_expected(spec),
                )
                return spec.public_summary()
            if spec.rotation_kind == "revocation_replacement":
                raise ProvisioningError(
                    "revocation replacement requires the predecessor run to be revoked first"
                )
            if not isinstance(predecessor_expires, datetime) or predecessor_expires > effective_now:
                raise ProvisioningError("a non-expired competing run is still running")
            if not isinstance(predecessor_revision, int):
                raise ProvisioningError("predecessor run revision is invalid")
            expire_parameters = {
                **parameters,
                "predecessor_run_id": predecessor_id,
                "predecessor_revision": predecessor_revision,
            }
            connection.execute(EXPIRE_RUNNING_RUN.sql, expire_parameters)
            expired = connection.execute(
                """
                SELECT status, ended_at, termination_code
                FROM public.anc_agent_runs
                WHERE company_id = %(company_id)s AND agent_run_id = %(predecessor_run_id)s
                """,
                expire_parameters,
            ).fetchone()
            expired_values = (
                None if expired is None else tuple(_normalize_row_value(value) for value in expired)
            )
            expected_expired = (
                "expired",
                spec.run_created_at,
                RUN_EXPIRED_TERMINATION,
            )
            if expired_values != expected_expired:
                raise ProvisioningError("expired predecessor run could not be retired")
        elif spec.rotation_kind == "expiry":
            existing = connection.execute(SELECT_RUN.sql, parameters).fetchone()
            if existing is None:
                raise ProvisioningError("expiry rotation requires an expired running predecessor")
            actual = tuple(_normalize_row_value(value) for value in existing)
            expected = tuple(_normalize_row_value(value) for value in _running_run_expected(spec))
            if actual != expected:
                raise ProvisioningError("existing agent run differs from the requested binding")
            return spec.public_summary()

        connection.execute(INSERT_SUCCESSOR_RUN.sql, parameters)
        _require_exact_row(
            connection.execute(SELECT_RUN.sql, parameters),
            record_name="agent run",
            expected=_running_run_expected(spec),
        )
    return spec.public_summary()


def _load_json(path_value: str, *, label: str) -> Mapping[str, object]:
    try:
        if path_value == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            content = stream.read(MAX_JSON_BYTES + 1)
            encoded = content if isinstance(content, bytes) else content.encode("utf-8")
        else:
            with Path(path_value).open("rb") as stream:
                encoded = stream.read(MAX_JSON_BYTES + 1)
        if len(encoded) > MAX_JSON_BYTES:
            raise ProvisioningError(f"{label} JSON exceeds {MAX_JSON_BYTES} bytes")
        content = encoded.decode("utf-8")

        def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise ProvisioningError(f"{label} contains a duplicate JSON key")
                result[key] = item
            return result

        value = json.loads(content, object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProvisioningError(f"could not read valid {label} JSON") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProvisioningError(f"{label} must be a JSON object")
    return value


def load_spec(
    manifest_path: str,
    definition_path: str,
    *,
    now: datetime | None = None,
    rotate: bool = False,
) -> ProvisioningSpec:
    if manifest_path == "-" and definition_path == "-":
        raise ProvisioningError("manifest and definition cannot both read from standard input")
    manifest = _load_json(manifest_path, label="manifest")
    definition = _load_json(definition_path, label="definition")
    return ProvisioningSpec.from_mapping(
        manifest,
        definition=definition,
        now=now,
        rotate=rotate,
    )


def database_url_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """Load an operator DSN without allowing it to appear in process arguments."""

    values = os.environ if environ is None else environ
    inline = values.get(DATABASE_URL_ENV)
    file_name = values.get(DATABASE_URL_FILE_ENV)
    if inline and file_name:
        raise ProvisioningError(f"set only one of {DATABASE_URL_ENV} and {DATABASE_URL_FILE_ENV}")
    if inline:
        if inline != inline.strip() or "\n" in inline or "\r" in inline or "\x00" in inline:
            raise ProvisioningError(f"{DATABASE_URL_ENV} must contain one canonical value")
        try:
            return require_tls_database_url(inline, field_name=DATABASE_URL_ENV)
        except ValueError as error:
            raise ProvisioningError(str(error)) from error
    if file_name:
        path = Path(file_name)
        if path.is_symlink():
            raise ProvisioningError(f"{DATABASE_URL_FILE_ENV} must not name a symlink")
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ProvisioningError(f"{DATABASE_URL_FILE_ENV} must name a regular 0600 file")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise ProvisioningError(
                    f"{DATABASE_URL_FILE_ENV} file must be owned by the current user"
                )
            value = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProvisioningError("could not read the provisioning database URL file") from error
        if value.endswith("\n"):
            value = value[:-1]
        if not value or value != value.strip() or "\n" in value or "\r" in value:
            raise ProvisioningError("provisioning database URL file must contain one value")
        try:
            return require_tls_database_url(value, field_name=DATABASE_URL_FILE_ENV)
        except ValueError as error:
            raise ProvisioningError(str(error)) from error
    raise ProvisioningError(
        f"set {DATABASE_URL_ENV} or {DATABASE_URL_FILE_ENV}; no database URL flag is accepted"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan, apply, or rotate one exact Grok/Supabase agent identity binding.",
    )
    parser.add_argument("command", choices=("plan", "apply", "rotate-plan", "rotate"))
    parser.add_argument(
        "--manifest",
        required=True,
        help="non-secret provisioning or rotation manifest JSON path, or - for stdin",
    )
    parser.add_argument(
        "--definition",
        required=True,
        help="reviewed agent-definition JSON path, or - for stdin",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rotate = args.command in {"rotate", "rotate-plan"}
    try:
        spec = load_spec(args.manifest, args.definition, rotate=rotate)
        if args.command in {"plan", "rotate-plan"}:
            print(json.dumps(parameterized_plan(spec, rotate=rotate), indent=2, sort_keys=True))
            return 0

        dsn = database_url_from_environment()
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - packaging failure
            raise ProvisioningError("apply requires the project psycopg dependency") from error
        try:
            with psycopg.connect(dsn) as connection:
                result = (
                    apply_rotation(spec, connection)
                    if rotate
                    else apply_provisioning(spec, connection)
                )
        except psycopg.Error as error:
            raise ProvisioningError(
                "database operation failed; the provisioning transaction was rolled back"
            ) from error
        status = "rotated" if rotate else "provisioned"
        print(json.dumps({"status": status, **result}, indent=2, sort_keys=True))
        return 0
    except ProvisioningError as error:
        print(f"provisioning failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

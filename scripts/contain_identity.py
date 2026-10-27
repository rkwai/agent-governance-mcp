#!/usr/bin/env python3
"""Plan and apply exact identity containment without deleting history.

Supports run revocation, subject disable/revoke, instance suspension,
OAuth-client disable, and bounded cell pause.  Each apply takes an advisory lock,
requires company scope, expected revision, parameter binding, and exactly one
affected row.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from scripts.operator_common import (
    OPERATOR_DATABASE_ROLE,
    Connection,
    OperatorError,
    canonical_uuid,
    connect_operator,
    database_url_from_environment,
    identifier,
    load_json_object,
    require_exact_fields,
    require_operator_role,
    revision,
    timestamp_text,
    utc_now,
)

ACTIONS = frozenset(
    {
        "revoke_run",
        "disable_subject",
        "revoke_subject",
        "suspend_instance",
        "disable_oauth_client",
        "pause_cell",
    }
)
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "revoked", "expired"})


@dataclass(frozen=True, slots=True)
class ContainmentSpec:
    action: str
    company_id: str
    expected_revision: int
    target_id: str
    termination_code: str | None
    expected_run_revision: int | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ContainmentSpec:
        required = {"action", "company_id", "expected_revision", "target_id"}
        optional = {"termination_code", "expected_run_revision"}
        require_exact_fields(value, required | (set(value) & optional), label="manifest")
        unknown = sorted(value.keys() - (required | optional))
        if unknown:
            raise OperatorError(f"manifest contains unknown fields: {', '.join(unknown)}")
        action = identifier(value["action"], "action")
        if action not in ACTIONS:
            raise OperatorError("action is not a supported containment action")
        termination_code = None
        if action == "revoke_run":
            raw_code = value.get("termination_code", "operator_revoked")
            termination_code = identifier(raw_code, "termination_code")
        elif "termination_code" in value:
            raise OperatorError("termination_code is only valid for revoke_run")
        expected_run_revision = None
        if action == "suspend_instance" and "expected_run_revision" in value:
            expected_run_revision = revision(
                value["expected_run_revision"],
                "expected_run_revision",
            )
        elif "expected_run_revision" in value:
            raise OperatorError("expected_run_revision is only valid for suspend_instance")
        if action in {"disable_subject", "revoke_subject"}:
            target_id = identifier(value["target_id"], "target_id")
        else:
            target_id = canonical_uuid(value["target_id"], "target_id")
        return cls(
            action=action,
            company_id=canonical_uuid(value["company_id"], "company_id"),
            expected_revision=revision(value["expected_revision"], "expected_revision"),
            target_id=target_id,
            termination_code=termination_code,
            expected_run_revision=expected_run_revision,
        )

    @property
    def lock_key(self) -> str:
        return f"anc-contain:{self.company_id}:{self.action}:{self.target_id}"

    def parameters(self, *, occurred_at: datetime) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "company_id": self.company_id,
                "target_id": self.target_id,
                "expected_revision": self.expected_revision,
                "expected_run_revision": self.expected_run_revision,
                "termination_code": self.termination_code,
                "occurred_at": occurred_at,
                "lock_key": self.lock_key,
            }
        )

    def public_summary(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "action": self.action,
                "company_id": self.company_id,
                "target_id": self.target_id,
                "expected_revision": self.expected_revision,
            }
        )


def parameterized_plan(spec: ContainmentSpec) -> Mapping[str, object]:
    return {
        "transaction": "required",
        "exact_idempotency": True,
        "history_rewrite": False,
        "database_role": OPERATOR_DATABASE_ROLE,
        "action": spec.action,
        "lock_key": spec.lock_key,
        "parameters": {
            "company_id": spec.company_id,
            "target_id": spec.target_id,
            "expected_revision": spec.expected_revision,
            "termination_code": spec.termination_code,
            "expected_run_revision": spec.expected_run_revision,
        },
        "postconditions": [
            "exactly one governed row matches the company, target, and expected revision",
            "terminal metadata is written without deleting or rewriting history",
            "stale revisions, wrong company, and zero/multiple row matches fail closed",
        ],
    }


def _one_row(row: Sequence[Any] | None, *, label: str) -> Sequence[Any]:
    if row is None:
        raise OperatorError(f"{label} was not found or expected revision did not match")
    return row


def _apply_revoke_run(
    spec: ContainmentSpec,
    connection: Connection,
    parameters: Mapping[str, object],
) -> Mapping[str, object]:
    current = connection.execute(
        """
        SELECT agent_run_id, status, revision, termination_code
        FROM public.anc_agent_runs
        WHERE company_id = %(company_id)s AND agent_run_id = %(target_id)s
        FOR UPDATE
        """,
        parameters,
    ).fetchone()
    row = _one_row(current, label="agent run")
    if row[1] == "revoked" and row[2] == spec.expected_revision and row[3] == spec.termination_code:
        return {"affected": 1, "status": "revoked", "idempotent": True}
    if row[1] != "running" or row[2] != spec.expected_revision:
        raise OperatorError("agent run is not the expected running revision")
    connection.execute(
        """
        UPDATE public.anc_agent_runs
        SET status = 'revoked',
            ended_at = %(occurred_at)s,
            termination_code = %(termination_code)s,
            revision = revision + 1,
            updated_at = %(occurred_at)s
        WHERE company_id = %(company_id)s
          AND agent_run_id = %(target_id)s
          AND status = 'running'
          AND revision = %(expected_revision)s
        """,
        parameters,
    )
    updated = connection.execute(
        """
        SELECT status, revision, termination_code
        FROM public.anc_agent_runs
        WHERE company_id = %(company_id)s AND agent_run_id = %(target_id)s
        """,
        parameters,
    ).fetchone()
    if updated != ("revoked", spec.expected_revision + 1, spec.termination_code):
        raise OperatorError("agent run revocation did not update exactly one running row")
    return {"affected": 1, "status": "revoked", "revision": spec.expected_revision + 1}


def _apply_subject(
    spec: ContainmentSpec,
    connection: Connection,
    parameters: Mapping[str, object],
    *,
    target_status: str,
) -> Mapping[str, object]:
    current = connection.execute(
        """
        SELECT auth_subject_id, status, revision, disabled_at
        FROM public.anc_auth_subjects
        WHERE company_id = %(company_id)s AND auth_subject_id = %(target_id)s
        FOR UPDATE
        """,
        parameters,
    ).fetchone()
    row = _one_row(current, label="authentication subject")
    if row[1] == target_status and row[2] == spec.expected_revision:
        return {"affected": 1, "status": target_status, "idempotent": True}
    if row[1] == "revoked" and target_status == "disabled":
        raise OperatorError("a revoked auth subject cannot be disabled")
    if row[1] != "active" or row[2] != spec.expected_revision:
        raise OperatorError("authentication subject is not the expected active revision")
    connection.execute(
        """
        UPDATE public.anc_auth_subjects
        SET status = %(target_status)s,
            disabled_at = %(occurred_at)s,
            revision = revision + 1,
            updated_at = %(occurred_at)s
        WHERE company_id = %(company_id)s
          AND auth_subject_id = %(target_id)s
          AND status = 'active'
          AND revision = %(expected_revision)s
        """,
        {**parameters, "target_status": target_status},
    )
    updated = connection.execute(
        """
        SELECT status, revision
        FROM public.anc_auth_subjects
        WHERE company_id = %(company_id)s AND auth_subject_id = %(target_id)s
        """,
        parameters,
    ).fetchone()
    if updated != (target_status, spec.expected_revision + 1):
        raise OperatorError("authentication subject update did not affect exactly one row")
    return {"affected": 1, "status": target_status, "revision": spec.expected_revision + 1}


def _apply_suspend_instance(
    spec: ContainmentSpec,
    connection: Connection,
    parameters: Mapping[str, object],
) -> Mapping[str, object]:
    running = connection.execute(
        """
        SELECT agent_run_id, revision
        FROM public.anc_agent_runs
        WHERE company_id = %(company_id)s
          AND agent_instance_id = %(target_id)s
          AND status = 'running'
        FOR UPDATE
        """,
        parameters,
    ).fetchone()
    if running is not None:
        if spec.expected_run_revision is None or running[1] != spec.expected_run_revision:
            raise OperatorError("suspend_instance requires the expected running-run revision")
        connection.execute(
            """
            UPDATE public.anc_agent_runs
            SET status = 'revoked',
                ended_at = %(occurred_at)s,
                termination_code = 'instance_suspended',
                revision = revision + 1,
                updated_at = %(occurred_at)s
            WHERE company_id = %(company_id)s
              AND agent_run_id = %(run_id)s
              AND status = 'running'
              AND revision = %(expected_run_revision)s
            """,
            {**parameters, "run_id": running[0]},
        )
    current = connection.execute(
        """
        SELECT agent_instance_id, status, revision
        FROM public.anc_agent_instances
        WHERE company_id = %(company_id)s AND agent_instance_id = %(target_id)s
        FOR UPDATE
        """,
        parameters,
    ).fetchone()
    row = _one_row(current, label="agent instance")
    if row[1] == "suspended" and row[2] == spec.expected_revision:
        return {"affected": 1, "status": "suspended", "idempotent": True}
    if row[1] != "active" or row[2] != spec.expected_revision:
        raise OperatorError("agent instance is not the expected active revision")
    connection.execute(
        """
        UPDATE public.anc_agent_instances
        SET status = 'suspended',
            revision = revision + 1,
            updated_at = %(occurred_at)s
        WHERE company_id = %(company_id)s
          AND agent_instance_id = %(target_id)s
          AND status = 'active'
          AND revision = %(expected_revision)s
        """,
        parameters,
    )
    updated = connection.execute(
        """
        SELECT status, revision
        FROM public.anc_agent_instances
        WHERE company_id = %(company_id)s AND agent_instance_id = %(target_id)s
        """,
        parameters,
    ).fetchone()
    if updated != ("suspended", spec.expected_revision + 1):
        raise OperatorError("agent instance suspension did not affect exactly one row")
    return {"affected": 1, "status": "suspended", "revision": spec.expected_revision + 1}


def _apply_disable_client(
    spec: ContainmentSpec,
    connection: Connection,
    parameters: Mapping[str, object],
) -> Mapping[str, object]:
    current = connection.execute(
        """
        SELECT client_id, status, revision
        FROM public.anc_oauth_clients
        WHERE company_id = %(company_id)s AND client_id = %(target_id)s
        FOR UPDATE
        """,
        parameters,
    ).fetchone()
    row = _one_row(current, label="OAuth client")
    if row[1] == "disabled" and row[2] == spec.expected_revision:
        return {"affected": 1, "status": "disabled", "idempotent": True}
    if row[1] != "active" or row[2] != spec.expected_revision:
        raise OperatorError("OAuth client is not the expected active revision")
    connection.execute(
        """
        UPDATE public.anc_oauth_clients
        SET status = 'disabled',
            revision = revision + 1,
            updated_at = %(occurred_at)s
        WHERE company_id = %(company_id)s
          AND client_id = %(target_id)s
          AND status = 'active'
          AND revision = %(expected_revision)s
        """,
        parameters,
    )
    updated = connection.execute(
        """
        SELECT status, revision
        FROM public.anc_oauth_clients
        WHERE company_id = %(company_id)s AND client_id = %(target_id)s
        """,
        parameters,
    ).fetchone()
    if updated != ("disabled", spec.expected_revision + 1):
        raise OperatorError("OAuth client disable did not affect exactly one row")
    return {"affected": 1, "status": "disabled", "revision": spec.expected_revision + 1}


def _pause_cell_sync(spec: ContainmentSpec, database_url: str) -> Mapping[str, object]:
    import asyncio

    from ai_nature_company.auth import (
        LocalTestAuthenticator,
        PrincipalRecord,
        ResourceScope,
        Role,
        ScopedRoleGrant,
    )
    from ai_nature_company.postgres import PostgresRepository
    from ai_nature_company.service import create_service

    async def _run() -> Mapping[str, object]:
        from psycopg_pool import AsyncConnectionPool

        async def configure(connection: object) -> None:
            session = await connection.execute("SELECT session_user, current_user")
            identities = await session.fetchone()
            if identities != (OPERATOR_DATABASE_ROLE, OPERATOR_DATABASE_ROLE):
                raise OperatorError(
                    f"database session_user and current_user must be {OPERATOR_DATABASE_ROLE}"
                )
            await connection.execute(f"SET ROLE {OPERATOR_DATABASE_ROLE}")
            await connection.execute("SET row_security = on")
            await connection.commit()

        pool = AsyncConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=1,
            open=False,
            configure=configure,
        )
        async with pool:
            repository = PostgresRepository(pool=pool)
            async with repository.transaction(spec.company_id) as tx:
                company = await tx.get_projection("company", spec.company_id)
                if company is None:
                    raise OperatorError("company must be bootstrapped before cell pause")
                founder_id = company["state"].get("founder_principal_id")
                if not isinstance(founder_id, str):
                    raise OperatorError("company founder_principal_id is missing")
                cell = await tx.get_projection("cell", spec.target_id)
                if cell is None:
                    raise OperatorError("cell was not found")
            credential = secrets.token_urlsafe(32)
            principal = LocalTestAuthenticator(
                {
                    credential: PrincipalRecord(
                        principal_id=founder_id,
                        grants=(
                            ScopedRoleGrant(Role.FOUNDER, ResourceScope.company(spec.company_id)),
                        ),
                    )
                }
            ).authenticate(credential)
            response = await create_service(store=repository).invoke(
                "cell.transition",
                principal,
                {
                    "idempotency_key": (
                        f"operator-pause-cell:{spec.target_id}:{spec.expected_revision}"
                    ),
                    "expected_revision": spec.expected_revision,
                    "correlation_id": str(uuid4()),
                    "operation_intent": "cell.transition",
                    "payload": {"cell_id": spec.target_id, "target_status": "paused"},
                },
            )
            state = response.get("state") if isinstance(response, Mapping) else None
            cell_state = state.get("cell") if isinstance(state, Mapping) else response.get("cell")
            if not isinstance(cell_state, Mapping):
                raise OperatorError("cell pause did not return cell state")
            if cell_state.get("status") not in {"paused", "PAUSED"} and str(
                cell_state.get("state")
            ) not in {"paused"}:
                # OfferCell uses state/status depending on serialization.
                status = cell_state.get("state") or cell_state.get("status")
                if str(status) != "paused":
                    raise OperatorError("cell pause did not reach the paused state")
            return {
                "affected": 1,
                "status": "paused",
                "revision": spec.expected_revision + 1,
                "replayed": bool(response.get("replayed")),
            }

    return asyncio.run(_run())


def apply_containment(
    spec: ContainmentSpec,
    connection: Connection,
    *,
    occurred_at: datetime | None = None,
    database_url: str | None = None,
) -> Mapping[str, object]:
    occurred = occurred_at or utc_now()
    parameters = spec.parameters(occurred_at=occurred)
    if spec.action == "pause_cell":
        if not database_url:
            raise OperatorError("pause_cell requires the operator database URL")
        require_operator_role(connection)
        return {**dict(spec.public_summary()), **_pause_cell_sync(spec, database_url)}

    with connection.transaction():
        require_operator_role(connection)
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            parameters,
        )
        connection.execute(
            "SELECT set_config('app.company_id', %(company_id)s, true)",
            parameters,
        )
        if spec.action == "revoke_run":
            result = _apply_revoke_run(spec, connection, parameters)
        elif spec.action == "disable_subject":
            result = _apply_subject(spec, connection, parameters, target_status="disabled")
        elif spec.action == "revoke_subject":
            result = _apply_subject(spec, connection, parameters, target_status="revoked")
        elif spec.action == "suspend_instance":
            result = _apply_suspend_instance(spec, connection, parameters)
        elif spec.action == "disable_oauth_client":
            result = _apply_disable_client(spec, connection, parameters)
        else:
            raise OperatorError("action is not a supported containment action")
    return {**dict(spec.public_summary()), **result, "occurred_at": timestamp_text(occurred)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply one exact identity containment action.",
    )
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--manifest", required=True, help="non-secret containment manifest JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = ContainmentSpec.from_mapping(load_json_object(args.manifest, label="manifest"))
        if args.command == "plan":
            print(json.dumps(parameterized_plan(spec), indent=2, sort_keys=True))
            return 0
        dsn = database_url_from_environment()
        try:
            with connect_operator(dsn) as connection:
                result = apply_containment(spec, connection, database_url=dsn)
        except Exception as error:
            if isinstance(error, OperatorError):
                raise
            raise OperatorError(
                "database operation failed; the containment transaction was rolled back"
            ) from error
        print(json.dumps({"status": "applied", **result}, indent=2, sort_keys=True, default=str))
        return 0
    except OperatorError as error:
        print(f"containment failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

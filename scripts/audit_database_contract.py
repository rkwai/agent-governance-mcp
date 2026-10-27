#!/usr/bin/env python3
"""Read-only database-contract audit for runtime and operator roles.

Verifies exact current_user, role flags and memberships, public-schema privileges,
governed table/sequence/function ACLs, forced RLS, resolver/trigger functions, and
rejects unexplained effective grants.  Never prints a DSN or secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from scripts.operator_common import (
    OPERATOR_DATABASE_ROLE,
    RUNTIME_DATABASE_ROLE,
    Connection,
    OperatorError,
    connect_operator,
    database_url_from_environment,
)

GOVERNED_TABLES = (
    "anc_stream_heads",
    "anc_domain_events",
    "anc_audit_events",
    "anc_projections",
    "anc_idempotency_records",
    "anc_budgets",
    "anc_budget_reservations",
    "anc_work_leases",
    "anc_oauth_clients",
    "anc_oauth_client_resource_aliases",
    "anc_auth_subjects",
    "anc_agent_instances",
    "anc_agent_runs",
    "anc_mcp_tool_calls",
)
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
RUNTIME_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "anc_stream_heads": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_domain_events": frozenset({"SELECT", "INSERT"}),
        "anc_audit_events": frozenset({"SELECT", "INSERT"}),
        "anc_projections": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_idempotency_records": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_budgets": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_budget_reservations": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_work_leases": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_oauth_clients": frozenset(),
        "anc_oauth_client_resource_aliases": frozenset(),
        "anc_auth_subjects": frozenset(),
        "anc_agent_instances": frozenset(),
        "anc_agent_runs": frozenset(),
        "anc_mcp_tool_calls": frozenset({"SELECT", "INSERT"}),
    }
)
OPERATOR_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "anc_stream_heads": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_domain_events": frozenset({"SELECT", "INSERT"}),
        "anc_audit_events": frozenset({"SELECT", "INSERT"}),
        "anc_projections": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_idempotency_records": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_budgets": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_budget_reservations": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_work_leases": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_oauth_clients": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_oauth_client_resource_aliases": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_auth_subjects": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_agent_instances": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_agent_runs": frozenset({"SELECT", "INSERT", "UPDATE"}),
        "anc_mcp_tool_calls": frozenset({"SELECT"}),
    }
)
SEQUENCES = (
    "anc_domain_events_position_seq",
    "anc_audit_events_position_seq",
    "anc_mcp_tool_calls_position_seq",
)
SEQUENCE_PRIVILEGES = ("SELECT", "USAGE", "UPDATE")
RUNTIME_SEQUENCE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "anc_domain_events_position_seq": frozenset({"SELECT", "USAGE"}),
        "anc_audit_events_position_seq": frozenset({"SELECT", "USAGE"}),
        "anc_mcp_tool_calls_position_seq": frozenset({"SELECT", "USAGE"}),
    }
)
OPERATOR_SEQUENCE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "anc_domain_events_position_seq": frozenset({"SELECT", "USAGE"}),
        "anc_audit_events_position_seq": frozenset({"SELECT", "USAGE"}),
        "anc_mcp_tool_calls_position_seq": frozenset(),
    }
)
RUNTIME_FUNCTIONS = (
    "anc_resolve_agent_identity(text,text,text,text,text)",
    "anc_inspect_agent_run_binding(text,text,text,text,text)",
    "anc_auth_catalog_compatible()",
    "anc_list_expiring_agent_runs()",
)
REQUIRED_FUNCTIONS = (
    "anc_resolve_agent_identity",
    "anc_inspect_agent_run_binding",
    "anc_auth_catalog_compatible",
    "anc_list_expiring_agent_runs",
    "anc_validate_mcp_tool_call_attribution",
    "anc_protect_agent_run",
    "anc_validate_agent_instance_subject",
    "anc_protect_oauth_client_resource_alias",
)
REQUIRED_TRIGGERS = (
    (
        "anc_oauth_client_resource_aliases",
        "anc_oauth_client_resource_aliases_identity_guard",
        "anc_protect_oauth_client_resource_alias",
    ),
    (
        "anc_mcp_tool_calls",
        "anc_mcp_tool_calls_attribution_guard",
        "anc_validate_mcp_tool_call_attribution",
    ),
    (
        "anc_agent_runs",
        "anc_agent_runs_lifecycle_guard",
        "anc_protect_agent_run",
    ),
    (
        "anc_agent_instances",
        "anc_agent_instances_subject_guard",
        "anc_validate_agent_instance_subject",
    ),
)


def _role_contract(role_name: str) -> Mapping[str, object]:
    if role_name == RUNTIME_DATABASE_ROLE:
        return {
            "must_login": True,
            "tables": RUNTIME_TABLE_PRIVILEGES,
            "sequences": RUNTIME_SEQUENCE_PRIVILEGES,
            "functions_execute": frozenset(RUNTIME_FUNCTIONS),
            "public_create": False,
        }
    if role_name == OPERATOR_DATABASE_ROLE:
        return {
            "must_login": True,
            "tables": OPERATOR_TABLE_PRIVILEGES,
            "sequences": OPERATOR_SEQUENCE_PRIVILEGES,
            "functions_execute": frozenset(),
            "public_create": False,
        }
    raise OperatorError("role must be ai_nature_company_runtime or ai_nature_company_operator")


def _as_privilege_set(rows: Sequence[Sequence[Any]]) -> dict[str, set[str]]:
    granted: dict[str, set[str]] = {}
    for name, privilege, is_granted in rows:
        granted.setdefault(str(name), set())
        if is_granted:
            granted[str(name)].add(str(privilege))
    return granted


def audit_database_contract(connection: Connection, *, role_name: str) -> Mapping[str, object]:
    """Inspect the connected role against the checked-in grant contract."""

    contract = _role_contract(role_name)
    current = connection.execute("SELECT current_user").fetchone()
    if current is None or current[0] != role_name:
        raise OperatorError(f"database current_user must be exactly {role_name}")

    flags = connection.execute(
        """
        SELECT role.rolcanlogin, role.rolsuper, role.rolbypassrls,
               role.rolcreatedb, role.rolcreaterole, role.rolreplication,
               role.rolinherit,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_auth_members AS membership
                   WHERE membership.member = role.oid
               ) AS has_membership
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_user
        """
    ).fetchone()
    if flags != (contract["must_login"], False, False, False, False, False, False, False):
        raise OperatorError(f"{role_name} role flags or memberships violate the contract")

    schema = connection.execute(
        """
        SELECT has_schema_privilege(current_user, 'public', 'USAGE'),
               has_schema_privilege(current_user, 'public', 'CREATE')
        """
    ).fetchone()
    if schema != (True, False):
        raise OperatorError(f"{role_name} must have public USAGE and must not CREATE in public")

    rls_rows = connection.execute(
        """
        SELECT relation.relname, relation.relrowsecurity, relation.relforcerowsecurity
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public' AND relation.relname = ANY(%(tables)s)
        ORDER BY relation.relname
        """,
        {"tables": list(GOVERNED_TABLES)},
    ).fetchall()
    if list(rls_rows) != [(table, True, True) for table in sorted(GOVERNED_TABLES)]:
        raise OperatorError("every governed table must exist with forced RLS")

    table_rows = connection.execute(
        """
        SELECT relation_name, privilege,
               has_table_privilege(
                   current_user,
                   format('public.%%I', relation_name),
                   privilege
               )
        FROM unnest(%(relations)s::text[]) AS relation(relation_name)
        CROSS JOIN unnest(%(privileges)s::text[]) AS action(privilege)
        """,
        {"relations": list(GOVERNED_TABLES), "privileges": list(TABLE_PRIVILEGES)},
    ).fetchall()
    actual_tables = _as_privilege_set(table_rows)
    expected_tables = {name: set(privileges) for name, privileges in contract["tables"].items()}
    if actual_tables != expected_tables:
        raise OperatorError(f"{role_name} table privileges differ from the documented contract")

    sequence_rows = connection.execute(
        """
        SELECT sequence_name, privilege,
               has_sequence_privilege(
                   current_user,
                   format('public.%%I', sequence_name),
                   privilege
               )
        FROM unnest(%(sequences)s::text[]) AS sequence(sequence_name)
        CROSS JOIN unnest(%(privileges)s::text[]) AS action(privilege)
        """,
        {"sequences": list(SEQUENCES), "privileges": list(SEQUENCE_PRIVILEGES)},
    ).fetchall()
    actual_sequences = _as_privilege_set(sequence_rows)
    expected_sequences = {
        name: set(privileges) for name, privileges in contract["sequences"].items()
    }
    if actual_sequences != expected_sequences:
        raise OperatorError(f"{role_name} sequence privileges differ from the documented contract")

    function_rows = connection.execute(
        """
        SELECT required.signature,
               to_regprocedure('public.' || required.signature) IS NOT NULL AS exists,
               COALESCE(
                   has_function_privilege(
                       current_user,
                       to_regprocedure('public.' || required.signature),
                       'EXECUTE'
                   ),
                   false
               ) AS can_execute
        FROM unnest(%(signatures)s::text[]) AS required(signature)
        """,
        {"signatures": list(RUNTIME_FUNCTIONS)},
    ).fetchall()
    expected_execute = contract["functions_execute"]
    for signature, exists, can_execute in function_rows:
        if exists is not True:
            raise OperatorError(f"required function {signature} is missing")
        if can_execute is not (signature in expected_execute):
            raise OperatorError(f"{role_name} EXECUTE grant on {signature} is incorrect")

    attribution_acl = connection.execute(
        """
        SELECT
            procedure.oid IS NOT NULL,
            COALESCE(
                (
                    SELECT bool_or(acl.grantee = 0 AND acl.privilege_type = 'EXECUTE')
                    FROM aclexplode(
                        COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
                    ) AS acl
                ),
                false
            )
        FROM (SELECT 1) AS required
        LEFT JOIN pg_catalog.pg_proc AS procedure
          ON procedure.oid = to_regprocedure(
              'public.anc_validate_mcp_tool_call_attribution()'
          )
        """
    ).fetchone()
    if attribution_acl != (True, False):
        raise OperatorError("anc_validate_mcp_tool_call_attribution must deny PUBLIC EXECUTE")

    missing_functions = connection.execute(
        """
        SELECT required.function_name
        FROM unnest(%(names)s::text[]) AS required(function_name)
        WHERE to_regprocedure(
            CASE required.function_name
                WHEN 'anc_resolve_agent_identity'
                    THEN 'public.anc_resolve_agent_identity(text,text,text,text,text)'
                WHEN 'anc_inspect_agent_run_binding'
                    THEN 'public.anc_inspect_agent_run_binding(text,text,text,text,text)'
                WHEN 'anc_auth_catalog_compatible'
                    THEN 'public.anc_auth_catalog_compatible()'
                WHEN 'anc_list_expiring_agent_runs'
                    THEN 'public.anc_list_expiring_agent_runs()'
                ELSE format('public.%%I()', required.function_name)
            END
        ) IS NULL
        """,
        {"names": list(REQUIRED_FUNCTIONS)},
    ).fetchall()
    if missing_functions:
        raise OperatorError("required resolver or trigger functions are missing")

    attached_triggers = connection.execute(
        """
        SELECT required.table_name, required.trigger_name, required.function_name
        FROM unnest(
            %(tables)s::text[],
            %(triggers)s::text[],
            %(functions)s::text[]
        ) AS required(table_name, trigger_name, function_name)
        JOIN pg_catalog.pg_trigger AS trigger
          ON trigger.tgname = required.trigger_name
         AND NOT trigger.tgisinternal
         AND trigger.tgenabled IN ('O', 'A')
        JOIN pg_catalog.pg_class AS relation
          ON relation.oid = trigger.tgrelid
         AND relation.relname = required.table_name
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
         AND namespace.nspname = 'public'
        JOIN pg_catalog.pg_proc AS procedure
          ON procedure.oid = trigger.tgfoid
         AND procedure.proname = required.function_name
        ORDER BY required.table_name, required.trigger_name
        """,
        {
            "tables": [item[0] for item in REQUIRED_TRIGGERS],
            "triggers": [item[1] for item in REQUIRED_TRIGGERS],
            "functions": [item[2] for item in REQUIRED_TRIGGERS],
        },
    ).fetchall()
    if {tuple(row) for row in attached_triggers} != set(REQUIRED_TRIGGERS):
        raise OperatorError(
            "required triggers are missing, disabled, or not bound to their functions"
        )

    return MappingProxyType(
        {
            "status": "pass",
            "role": role_name,
            "read_only": True,
            "public_schema_create": False,
            "forced_rls": True,
            "attribution_public_execute": False,
            "required_triggers": True,
            "tables": {name: sorted(privileges) for name, privileges in expected_tables.items()},
            "sequences": {
                name: sorted(privileges) for name, privileges in expected_sequences.items()
            },
        }
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only audit of the runtime or operator database contract.",
    )
    parser.add_argument("command", choices=("audit",))
    parser.add_argument(
        "--role",
        choices=(RUNTIME_DATABASE_ROLE, OPERATOR_DATABASE_ROLE),
        required=True,
        help="exact role the connected DSN must be using",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        dsn = database_url_from_environment()
        try:
            with connect_operator(dsn) as connection:
                report = audit_database_contract(connection, role_name=args.role)
        except Exception as error:
            if isinstance(error, OperatorError):
                raise
            raise OperatorError("database contract audit failed") from error
        print(json.dumps(dict(report), indent=2, sort_keys=True))
        return 0
    except OperatorError as error:
        print(f"audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

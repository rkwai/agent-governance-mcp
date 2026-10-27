"""Fail-closed comparison of shipped SQL migrations against a live database.

Railway runs the MCP process only.  Supabase migrations are applied out of band
and recorded in ``supabase_migrations.schema_migrations`` under provider-assigned
versions that do not match repository filename prefixes.  Readiness therefore
matches applied rows by **name/slug**, not version equality, and independently
fingerprints required schema objects through ``pg_catalog``.

The Railway runtime role (``ai_nature_company_runtime``) is least-privilege: it
has no ``USAGE`` on ``supabase_migrations`` and no ``SELECT`` on
``schema_migrations``.  The ledger probe therefore checks
``has_schema_privilege`` / ``has_table_privilege`` first and, on a
non-autocommit connection, wraps any follow-up read in a savepoint so a denied
``SELECT`` cannot abort the readiness transaction.  When the ledger is
unreadable, ``applied_names`` is ``None`` and the ``pg_catalog`` fingerprint is
the fail-closed gate.  ``pg_catalog`` remains readable to that role.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol

_FILENAME_VERSION_PREFIX = re.compile(r"^(?P<prefix>\d+)_(?P<slug>.+)$")
_MIGRATIONS_ENV = "ANC_MIGRATIONS_DIR"
_PACKAGED_MIGRATIONS = "shipped_migrations"

FactKind = Literal["table", "column", "constraint", "function", "role"]


class MigrationConnection(Protocol):
    async def execute(
        self,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ShippedMigration:
    filename: str
    slug: str
    version_prefix: str

    @property
    def stem(self) -> str:
        return f"{self.version_prefix}_{self.slug}"


@dataclass(frozen=True, slots=True)
class SchemaFact:
    """One catalog object a shipped migration must leave behind (or remove)."""

    slug: str
    kind: FactKind
    name: str
    relation: str | None = None
    required: bool = True
    definition_contains: str | None = None

    @property
    def label(self) -> str:
        if self.kind in {"column", "constraint"} and self.relation:
            return f"{self.kind} public.{self.relation}.{self.name}"
        if self.kind == "table":
            return f"table public.{self.name}"
        if self.kind == "function":
            return f"function public.{self.name}"
        if self.kind == "role":
            return f"role {self.name}"
        return f"{self.kind} {self.name}"


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    tables: frozenset[str]
    columns: frozenset[tuple[str, str]]
    constraints: dict[tuple[str, str], str]
    functions: frozenset[str]
    roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class MigrationReadinessReport:
    ready: bool
    shipped_slugs: tuple[str, ...]
    applied_names_readable: bool
    missing_migrations: tuple[str, ...]
    missing_schema_facts: tuple[str, ...]
    forbidden_schema_facts_present: tuple[str, ...]
    message: str

    def as_ready_payload(self) -> dict[str, object]:
        return {
            "reason": "database_migrations_unapplied",
            "message": self.message,
            "missing_migrations": list(self.missing_migrations),
            "missing_schema_facts": list(self.missing_schema_facts),
            "forbidden_schema_facts_present": list(self.forbidden_schema_facts_present),
        }


def migration_slug_from_filename(filename: str) -> ShippedMigration:
    stem = Path(filename).stem
    match = _FILENAME_VERSION_PREFIX.fullmatch(stem)
    if match is None:
        raise ValueError(f"migration filename must be <digits>_<slug>.sql: {filename}")
    return ShippedMigration(
        filename=Path(filename).name,
        slug=match.group("slug"),
        version_prefix=match.group("prefix"),
    )


def applied_name_covers_slug(
    applied_names: Iterable[str],
    *,
    slug: str,
    filename_stem: str,
) -> bool:
    """Match provider ledger names; never compare version prefixes for equality."""

    return any(name in (slug, filename_stem) or name.endswith(f"_{slug}") for name in applied_names)


def resolve_migrations_dir(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get(_MIGRATIONS_ENV)
    if configured:
        path = Path(configured)
        if path.is_dir():
            return path
        raise RuntimeError(f"{_MIGRATIONS_ENV} is not a directory")

    packaged = _packaged_migrations_dir()
    if packaged is not None:
        return packaged

    cwd_dir = Path.cwd() / "supabase" / "migrations"
    if cwd_dir.is_dir() and any(cwd_dir.glob("*.sql")):
        return cwd_dir

    source_tree = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
    if source_tree.is_dir() and any(source_tree.glob("*.sql")):
        return source_tree

    raise RuntimeError("shipped migrations directory is missing from the deploy image")


def shipped_migrations(directory: Path | None = None) -> tuple[ShippedMigration, ...]:
    root = directory if directory is not None else resolve_migrations_dir()
    files = sorted(root.glob("*.sql"))
    if not files:
        raise RuntimeError(f"no shipped *.sql migrations in {root}")
    return tuple(migration_slug_from_filename(path.name) for path in files)


def _packaged_migrations_dir() -> Path | None:
    try:
        traversable = resources.files("ai_nature_company").joinpath(_PACKAGED_MIGRATIONS)
    except ModuleNotFoundError:
        return None
    if not traversable.is_dir():
        return None
    with resources.as_file(traversable) as extracted:
        path = Path(extracted)
        if path.is_dir() and any(path.glob("*.sql")):
            return path
    return None


REQUIRED_SCHEMA_FACTS: tuple[SchemaFact, ...] = (
    SchemaFact("phase1_persistence", "table", "anc_stream_heads"),
    SchemaFact("phase1_persistence", "table", "anc_domain_events"),
    SchemaFact("phase1_persistence", "table", "anc_audit_events"),
    SchemaFact("phase1_persistence", "table", "anc_projections"),
    SchemaFact("phase1_persistence", "table", "anc_idempotency_records"),
    SchemaFact("phase1_persistence", "table", "anc_budgets"),
    SchemaFact("phase1_persistence", "table", "anc_budget_reservations"),
    SchemaFact("phase1_persistence", "table", "anc_work_leases"),
    SchemaFact(
        "phase1_persistence",
        "column",
        "actual_amount",
        relation="anc_budget_reservations",
    ),
    SchemaFact("phase1_persistence", "function", "anc_reject_append_only_mutation"),
    SchemaFact(
        "hierarchical_budget_ledgers",
        "column",
        "parent_budget_id",
        relation="anc_budgets",
    ),
    SchemaFact("hierarchical_budget_ledgers", "column", "stage", relation="anc_budgets"),
    SchemaFact(
        "hierarchical_budget_ledgers",
        "column",
        "reservation_group_id",
        relation="anc_budget_reservations",
    ),
    SchemaFact(
        "hierarchical_budget_ledgers",
        "column",
        "ledger_role",
        relation="anc_budget_reservations",
    ),
    SchemaFact(
        "hierarchical_budget_ledgers",
        "constraint",
        "anc_budgets_parent_fk",
        relation="anc_budgets",
    ),
    SchemaFact(
        "hierarchical_budget_ledgers",
        "constraint",
        "anc_budgets_stage_scope_check",
        relation="anc_budgets",
    ),
    SchemaFact(
        "hierarchical_budget_ledgers",
        "constraint",
        "anc_budget_reservations_ledger_role_check",
        relation="anc_budget_reservations",
    ),
    SchemaFact("agent_identity_and_attribution", "table", "anc_oauth_clients"),
    SchemaFact("agent_identity_and_attribution", "table", "anc_auth_subjects"),
    SchemaFact("agent_identity_and_attribution", "table", "anc_agent_instances"),
    SchemaFact("agent_identity_and_attribution", "table", "anc_agent_runs"),
    SchemaFact("agent_identity_and_attribution", "table", "anc_mcp_tool_calls"),
    SchemaFact("agent_identity_and_attribution", "function", "anc_resolve_agent_identity"),
    SchemaFact("agent_identity_and_attribution", "function", "anc_auth_catalog_compatible"),
    SchemaFact(
        "agent_identity_and_attribution",
        "function",
        "anc_validate_mcp_tool_call_attribution",
    ),
    SchemaFact("runtime_role_least_privilege", "role", "ai_nature_company_runtime"),
    SchemaFact("operator_role", "role", "ai_nature_company_operator"),
    SchemaFact(
        "closed_beta_authority_hardening",
        "constraint",
        "anc_agent_instances_active_cell_owner_requires_cell",
        relation="anc_agent_instances",
    ),
    SchemaFact("oauth_client_resource_aliases", "table", "anc_oauth_client_resource_aliases"),
    SchemaFact("budget_units_of_account", "column", "unit", relation="anc_budgets"),
    SchemaFact(
        "budget_units_of_account",
        "column",
        "unit",
        relation="anc_budget_reservations",
    ),
    SchemaFact(
        "budget_units_of_account",
        "constraint",
        "anc_budgets_unit_check",
        relation="anc_budgets",
    ),
    SchemaFact(
        "budget_units_of_account",
        "constraint",
        "anc_budgets_unit_currency_check",
        relation="anc_budgets",
    ),
    SchemaFact(
        "budget_units_of_account",
        "constraint",
        "anc_budget_reservations_unit_check",
        relation="anc_budget_reservations",
    ),
    SchemaFact(
        "budget_units_of_account",
        "constraint",
        "anc_budget_reservations_unit_currency_check",
        relation="anc_budget_reservations",
    ),
    SchemaFact(
        "oauth_path_resource_aliases",
        "constraint",
        "anc_oauth_client_resource_aliases_resource_url_check",
        relation="anc_oauth_client_resource_aliases",
        definition_contains="/mcp(?:/[a-z0-9]+",
    ),
    SchemaFact(
        "active_reservation_partial_spend",
        "constraint",
        "anc_budget_reservations_active_partial_spend_check",
        relation="anc_budget_reservations",
    ),
    SchemaFact(
        "active_reservation_partial_spend",
        "constraint",
        "anc_budget_reservations_check2",
        relation="anc_budget_reservations",
        required=False,
    ),
    SchemaFact(
        "agent_run_expiry_diagnostics",
        "function",
        "anc_inspect_agent_run_binding",
    ),
    SchemaFact(
        "agent_run_expiry_diagnostics",
        "function",
        "anc_list_expiring_agent_runs",
    ),
    SchemaFact(
        "runtime_tick_maintenance_report",
        "function",
        "anc_list_tick_company_ids",
    ),
    SchemaFact(
        "runtime_tick_maintenance_report",
        "function",
        "anc_list_maintenance_agent_runs",
    ),
    SchemaFact(
        "runtime_tick_keyset_pages",
        "function",
        "anc_list_tick_company_ids_page",
    ),
    SchemaFact(
        "runtime_tick_keyset_pages",
        "function",
        "anc_list_maintenance_agent_runs_page",
    ),
    SchemaFact(
        "cell_kill_ends_agent_runs",
        "function",
        "anc_end_cell_agent_runs",
    ),
)


def evaluate_migration_readiness(
    *,
    shipped: Sequence[ShippedMigration],
    applied_names: frozenset[str] | None,
    snapshot: SchemaSnapshot,
    facts: Sequence[SchemaFact] = REQUIRED_SCHEMA_FACTS,
) -> MigrationReadinessReport:
    missing_names: list[str] = []
    if applied_names is not None:
        for migration in shipped:
            if not applied_name_covers_slug(
                applied_names,
                slug=migration.slug,
                filename_stem=migration.stem,
            ):
                missing_names.append(migration.slug)

    missing_facts: list[str] = []
    forbidden_present: list[str] = []
    slugs_with_fact_failures: set[str] = set()
    for fact in facts:
        present = _fact_is_present(snapshot, fact)
        if fact.required and not present:
            missing_facts.append(fact.label)
            slugs_with_fact_failures.add(fact.slug)
        elif not fact.required and present:
            forbidden_present.append(fact.label)
            slugs_with_fact_failures.add(fact.slug)

    missing_migrations = tuple(dict.fromkeys([*missing_names, *sorted(slugs_with_fact_failures)]))
    ready = not missing_migrations and not missing_facts and not forbidden_present
    if ready:
        message = "database migrations match the shipped inventory"
    else:
        parts = []
        if missing_migrations:
            parts.append("missing_migrations=[" + ", ".join(missing_migrations) + "]")
        if missing_facts:
            parts.append("missing_schema_facts=[" + ", ".join(missing_facts) + "]")
        if forbidden_present:
            parts.append("forbidden_schema_facts_present=[" + ", ".join(forbidden_present) + "]")
        message = "database migrations unapplied: " + "; ".join(parts)

    return MigrationReadinessReport(
        ready=ready,
        shipped_slugs=tuple(migration.slug for migration in shipped),
        applied_names_readable=applied_names is not None,
        missing_migrations=missing_migrations,
        missing_schema_facts=tuple(missing_facts),
        forbidden_schema_facts_present=tuple(forbidden_present),
        message=message,
    )


def _fact_is_present(snapshot: SchemaSnapshot, fact: SchemaFact) -> bool:
    if fact.kind == "table":
        return fact.name in snapshot.tables
    if fact.kind == "column":
        return fact.relation is not None and (fact.relation, fact.name) in snapshot.columns
    if fact.kind == "function":
        return fact.name in snapshot.functions
    if fact.kind == "role":
        return fact.name in snapshot.roles
    if fact.kind == "constraint":
        if fact.relation is None:
            return False
        definition = snapshot.constraints.get((fact.relation, fact.name))
        if definition is None:
            return False
        return not (fact.definition_contains and fact.definition_contains not in definition)
    return False


_LEDGER_SAVEPOINT = "anc_applied_migration_names"


def _connection_uses_transaction(connection: object) -> bool:
    return getattr(connection, "autocommit", True) is False


async def _ledger_is_readable(connection: MigrationConnection) -> bool:
    """True only when USAGE+SELECT are already granted; never probes the table."""

    cursor = await connection.execute(
        """
        SELECT COALESCE(
                   has_schema_privilege(current_user, 'supabase_migrations', 'USAGE'),
                   false
               )
               AND COALESCE(
                   has_table_privilege(
                       current_user,
                       'supabase_migrations.schema_migrations',
                       'SELECT'
                   ),
                   false
               ) AS ledger_readable
        -- anc_applied_migration_privilege
        """
    )
    return await cursor.fetchone() == (True,)


async def load_applied_migration_names(connection: MigrationConnection) -> frozenset[str] | None:
    """Return ledger names when readable; None if the runtime cannot see them.

    Must not abort a non-autocommit readiness transaction.  Production runtime
    and operator roles have no schema USAGE and no table SELECT, so this returns
    None after the privilege probe and never issues the failing SELECT.
    """

    use_savepoint = _connection_uses_transaction(connection)
    if use_savepoint:
        await connection.execute(f"SAVEPOINT {_LEDGER_SAVEPOINT}")
    try:
        if not await _ledger_is_readable(connection):
            names: frozenset[str] | None = None
        else:
            cursor = await connection.execute(
                """
                SELECT name
                FROM supabase_migrations.schema_migrations
                WHERE name IS NOT NULL AND btrim(name) <> ''
                -- anc_applied_migration_names
                """
            )
            rows = await cursor.fetchall()
            names = frozenset(str(row[0]) for row in rows if row and row[0])
    except Exception:
        names = None
        if use_savepoint:
            await connection.execute(f"ROLLBACK TO SAVEPOINT {_LEDGER_SAVEPOINT}")
        return None
    if use_savepoint:
        await connection.execute(f"RELEASE SAVEPOINT {_LEDGER_SAVEPOINT}")
    return names


async def inspect_schema(
    connection: MigrationConnection,
    *,
    facts: Sequence[SchemaFact] = REQUIRED_SCHEMA_FACTS,
) -> SchemaSnapshot:
    constraint_names = tuple(
        dict.fromkeys(fact.name for fact in facts if fact.kind == "constraint")
    )
    column_relations = tuple(
        dict.fromkeys(fact.relation for fact in facts if fact.kind == "column" and fact.relation)
    )
    table_names = tuple(dict.fromkeys(fact.name for fact in facts if fact.kind == "table"))
    function_names = tuple(dict.fromkeys(fact.name for fact in facts if fact.kind == "function"))
    role_names = tuple(dict.fromkeys(fact.name for fact in facts if fact.kind == "role"))
    cursor = await connection.execute(
        """
        SELECT 'constraint'::text AS kind,
               c.relname::text AS relation,
               con.conname::text AS name,
               pg_catalog.pg_get_constraintdef(con.oid)::text AS definition
        FROM pg_catalog.pg_constraint AS con
        JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND con.conname = ANY(%s)
        UNION ALL
        SELECT 'column',
               c.relname::text,
               a.attname::text,
               ''::text
        FROM pg_catalog.pg_attribute AS a
        JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND c.relkind IN ('r', 'p')
          AND c.relname = ANY(%s)
        UNION ALL
        SELECT 'table',
               c.relname::text,
               c.relname::text,
               ''::text
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relname = ANY(%s)
        UNION ALL
        SELECT 'function',
               ''::text,
               p.proname::text,
               ''::text
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = ANY(%s)
        UNION ALL
        SELECT 'role',
               ''::text,
               r.rolname::text,
               ''::text
        FROM pg_catalog.pg_roles AS r
        WHERE r.rolname = ANY(%s)
        -- anc_schema_fingerprint
        """,
        (
            list(constraint_names),
            list(column_relations),
            list(table_names),
            list(function_names),
            list(role_names),
        ),
    )
    rows = await cursor.fetchall()
    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    constraints: dict[tuple[str, str], str] = {}
    functions: set[str] = set()
    roles: set[str] = set()
    for row in rows:
        kind, relation, name, definition = row
        if kind == "table":
            tables.add(str(name))
        elif kind == "column":
            columns.add((str(relation), str(name)))
        elif kind == "constraint":
            constraints[(str(relation), str(name))] = str(definition or "")
        elif kind == "function":
            functions.add(str(name))
        elif kind == "role":
            roles.add(str(name))
    return SchemaSnapshot(
        tables=frozenset(tables),
        columns=frozenset(columns),
        constraints=constraints,
        functions=frozenset(functions),
        roles=frozenset(roles),
    )


async def assess_database_migrations(
    connection: MigrationConnection,
    *,
    shipped: Sequence[ShippedMigration] | None = None,
    facts: Sequence[SchemaFact] = REQUIRED_SCHEMA_FACTS,
) -> MigrationReadinessReport:
    inventory = tuple(shipped) if shipped is not None else shipped_migrations()
    applied_names = await load_applied_migration_names(connection)
    snapshot = await inspect_schema(connection, facts=facts)
    return evaluate_migration_readiness(
        shipped=inventory,
        applied_names=applied_names,
        snapshot=snapshot,
        facts=facts,
    )


__all__ = [
    "REQUIRED_SCHEMA_FACTS",
    "MigrationReadinessReport",
    "SchemaFact",
    "SchemaSnapshot",
    "ShippedMigration",
    "applied_name_covers_slug",
    "assess_database_migrations",
    "evaluate_migration_readiness",
    "inspect_schema",
    "load_applied_migration_names",
    "migration_slug_from_filename",
    "resolve_migrations_dir",
    "shipped_migrations",
]

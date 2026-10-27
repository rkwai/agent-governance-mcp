"""Contracts for shipped-migration discovery and fail-closed readiness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_nature_company.migration_readiness import (
    REQUIRED_SCHEMA_FACTS,
    SchemaSnapshot,
    applied_name_covers_slug,
    assess_database_migrations,
    evaluate_migration_readiness,
    migration_slug_from_filename,
    resolve_migrations_dir,
    shipped_migrations,
)

LIVE_APPLIED_NAMES = frozenset(
    {
        "phase1_persistence",
        "hierarchical_budget_ledgers",
        "agent_identity_and_attribution",
        "runtime_role_least_privilege",
        "operator_role",
        "closed_beta_authority_hardening",
        "oauth_client_resource_aliases",
        "budget_units_of_account",
        "oauth_path_resource_aliases",
        "active_reservation_partial_spend",
        "agent_run_expiry_diagnostics",
        "runtime_tick_maintenance_report",
        "runtime_tick_keyset_pages",
        "cell_kill_ends_agent_runs",
    }
)
LIVE_APPLIED_VERSIONS = frozenset(
    {
        "20260831040448",
        "20260831040535",
        "20260831040724",
        "20260831040740",
        "20260831040756",
        "20260831040827",
        "20260831084238",
        "20260831101825",
        "20260831104734",
        "20260927041044",
    }
)


def _complete_snapshot() -> SchemaSnapshot:
    tables = set()
    columns = set()
    constraints: dict[tuple[str, str], str] = {}
    functions = set()
    roles = set()
    for fact in REQUIRED_SCHEMA_FACTS:
        if not fact.required:
            continue
        if fact.kind == "table":
            tables.add(fact.name)
        elif fact.kind == "column" and fact.relation is not None:
            columns.add((fact.relation, fact.name))
        elif fact.kind == "constraint" and fact.relation is not None:
            constraints[(fact.relation, fact.name)] = fact.definition_contains or "present"
        elif fact.kind == "function":
            functions.add(fact.name)
        elif fact.kind == "role":
            roles.add(fact.name)
    return SchemaSnapshot(
        tables=frozenset(tables),
        columns=frozenset(columns),
        constraints=constraints,
        functions=frozenset(functions),
        roles=frozenset(roles),
    )


def test_shipped_migration_slugs_ignore_repo_version_prefixes() -> None:
    shipped = shipped_migrations()
    assert {item.slug for item in shipped} == LIVE_APPLIED_NAMES
    assert {item.version_prefix for item in shipped}.isdisjoint(LIVE_APPLIED_VERSIONS)
    partial = next(item for item in shipped if item.slug == "active_reservation_partial_spend")
    assert partial.filename == "202609270001_active_reservation_partial_spend.sql"
    assert partial.version_prefix == "202609270001"


def test_resolve_migrations_dir_finds_repo_or_packaged_sql() -> None:
    directory = resolve_migrations_dir()
    assert directory.is_dir()
    assert (directory / "202609270001_active_reservation_partial_spend.sql").is_file()


def test_applied_name_covers_slug_without_version_equality() -> None:
    assert applied_name_covers_slug(
        LIVE_APPLIED_NAMES,
        slug="active_reservation_partial_spend",
        filename_stem="202609270001_active_reservation_partial_spend",
    )
    assert not applied_name_covers_slug(
        LIVE_APPLIED_VERSIONS,
        slug="active_reservation_partial_spend",
        filename_stem="202609270001_active_reservation_partial_spend",
    )
    assert applied_name_covers_slug(
        {"20260927041044_active_reservation_partial_spend"},
        slug="active_reservation_partial_spend",
        filename_stem="202609270001_active_reservation_partial_spend",
    )


def test_every_repo_migration_file_has_a_schema_fact() -> None:
    """CI guard: fingerprint is the production gate, so every file needs a fact."""

    files = sorted(Path("supabase/migrations").glob("*.sql"))
    assert files, "supabase/migrations must contain shipped SQL"
    fact_slugs = {fact.slug for fact in REQUIRED_SCHEMA_FACTS}
    missing = [
        path.name
        for path in files
        if migration_slug_from_filename(path.name).slug not in fact_slugs
    ]
    assert missing == [], (
        "every supabase/migrations file needs a REQUIRED_SCHEMA_FACTS slug: " + ", ".join(missing)
    )


def test_live_provider_names_and_complete_fingerprint_pass() -> None:
    report = evaluate_migration_readiness(
        shipped=shipped_migrations(),
        applied_names=LIVE_APPLIED_NAMES,
        snapshot=_complete_snapshot(),
    )

    assert report.ready is True
    assert report.missing_migrations == ()
    assert report.applied_names_readable is True


def test_runtime_without_ledger_access_still_passes_complete_fingerprint() -> None:
    report = evaluate_migration_readiness(
        shipped=shipped_migrations(),
        applied_names=None,
        snapshot=_complete_snapshot(),
    )

    assert report.ready is True
    assert report.applied_names_readable is False


def test_incident_missing_partial_spend_is_listed_even_when_versions_differ() -> None:
    snapshot = _complete_snapshot()
    snapshot.constraints.pop(
        ("anc_budget_reservations", "anc_budget_reservations_active_partial_spend_check"),
        None,
    )
    snapshot.constraints[("anc_budget_reservations", "anc_budget_reservations_check2")] = (
        "CHECK ((status = 'active' AND actual_amount IS NULL))"
    )

    report = evaluate_migration_readiness(
        shipped=shipped_migrations(),
        applied_names=LIVE_APPLIED_NAMES - {"active_reservation_partial_spend"},
        snapshot=snapshot,
    )

    assert report.ready is False
    assert report.missing_migrations == ("active_reservation_partial_spend",)
    assert (
        "constraint public.anc_budget_reservations."
        "anc_budget_reservations_active_partial_spend_check" in report.missing_schema_facts
    )
    assert (
        "constraint public.anc_budget_reservations.anc_budget_reservations_check2"
        in report.forbidden_schema_facts_present
    )
    payload = report.as_ready_payload()
    assert payload["reason"] == "database_migrations_unapplied"
    assert payload["missing_migrations"] == ["active_reservation_partial_spend"]
    assert "active_reservation_partial_spend" in str(payload["message"])


def test_name_ledger_cannot_hide_missing_schema_fact() -> None:
    snapshot = _complete_snapshot()
    snapshot.constraints.pop(
        ("anc_budget_reservations", "anc_budget_reservations_active_partial_spend_check"),
        None,
    )

    report = evaluate_migration_readiness(
        shipped=shipped_migrations(),
        applied_names=LIVE_APPLIED_NAMES,
        snapshot=snapshot,
    )

    assert report.ready is False
    assert "active_reservation_partial_spend" in report.missing_migrations


def test_path_alias_constraint_requires_updated_definition() -> None:
    snapshot = _complete_snapshot()
    snapshot.constraints[
        (
            "anc_oauth_client_resource_aliases",
            "anc_oauth_client_resource_aliases_resource_url_check",
        )
    ] = "CHECK (resource_url ~ '^https://[^?#]+/mcp$')"

    report = evaluate_migration_readiness(
        shipped=shipped_migrations(),
        applied_names=LIVE_APPLIED_NAMES,
        snapshot=snapshot,
    )

    assert report.ready is False
    assert "oauth_path_resource_aliases" in report.missing_migrations


def test_filename_without_version_prefix_is_rejected() -> None:
    with pytest.raises(ValueError, match="digits"):
        migration_slug_from_filename("active_reservation_partial_spend.sql")


def test_configured_migrations_dir_must_exist(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ANC_MIGRATIONS_DIR"):
        resolve_migrations_dir({"ANC_MIGRATIONS_DIR": str(tmp_path / "missing")})


class _DeniedLedgerConnection:
    """Non-autocommit connection: ledger SELECT would abort, privilege probe must not."""

    def __init__(self) -> None:
        self.autocommit = False
        self.queries: list[str] = []

    async def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Any:
        del parameters
        self.queries.append(query)
        stripped = query.strip()
        if stripped.startswith(("SAVEPOINT ", "ROLLBACK TO SAVEPOINT ", "RELEASE SAVEPOINT ")):
            return _Rows([])
        if "anc_applied_migration_privilege" in query:
            return _Rows([(False,)])
        if "anc_applied_migration_names" in query:
            raise RuntimeError("permission denied for schema supabase_migrations")
        if "anc_schema_fingerprint" in query:
            rows = []
            for fact in REQUIRED_SCHEMA_FACTS:
                if not fact.required:
                    continue
                relation = fact.relation or (fact.name if fact.kind == "table" else "")
                rows.append((fact.kind, relation, fact.name, fact.definition_contains or ""))
            return _Rows(rows)
        raise AssertionError(f"unexpected query: {query}")


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)


@pytest.mark.asyncio
async def test_denied_ledger_select_does_not_block_fingerprint() -> None:
    connection = _DeniedLedgerConnection()

    report = await assess_database_migrations(connection)

    assert report.ready is True
    assert report.applied_names_readable is False
    assert any("SAVEPOINT" in query for query in connection.queries)
    assert any("RELEASE SAVEPOINT" in query for query in connection.queries)
    assert not any(
        "FROM supabase_migrations.schema_migrations" in query for query in connection.queries
    )
    assert any("anc_schema_fingerprint" in query for query in connection.queries)


class _AbortingLedgerConnection(_DeniedLedgerConnection):
    """Privilege probe lies; the SELECT fails and must stay inside the savepoint."""

    async def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Any:
        if "anc_applied_migration_privilege" in query:
            self.queries.append(query)
            return _Rows([(True,)])
        return await super().execute(query, parameters)


@pytest.mark.asyncio
async def test_failed_ledger_select_rolls_back_savepoint_then_fingerprints() -> None:
    connection = _AbortingLedgerConnection()

    report = await assess_database_migrations(connection)

    assert report.ready is True
    assert report.applied_names_readable is False
    assert any("ROLLBACK TO SAVEPOINT" in query for query in connection.queries)
    assert any("anc_schema_fingerprint" in query for query in connection.queries)

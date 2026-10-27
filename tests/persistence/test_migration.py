from pathlib import Path

MIGRATION = Path("supabase/migrations/202608290001_phase1_persistence.sql")
HIERARCHY_MIGRATION = Path("supabase/migrations/202608290002_hierarchical_budget_ledgers.sql")
UNITS_MIGRATION = Path("supabase/migrations/202608310008_budget_units_of_account.sql")
TABLES = {
    "anc_stream_heads",
    "anc_domain_events",
    "anc_audit_events",
    "anc_projections",
    "anc_idempotency_records",
    "anc_budgets",
    "anc_budget_reservations",
    "anc_work_leases",
}


def test_migration_is_additive_and_defines_all_phase_one_tables() -> None:
    sql = MIGRATION.read_text()
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS public.{table}" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql


def test_every_table_enables_and_forces_rls() -> None:
    sql = MIGRATION.read_text()
    for table in TABLES:
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in sql
    assert "current_setting('app.company_id', true)" in sql
    assert "NULLIF" in sql


def test_event_tables_are_append_only_with_no_mutation_policy() -> None:
    sql = MIGRATION.read_text()
    assert "anc_reject_append_only_mutation" in sql
    assert "BEFORE UPDATE OR DELETE ON public.anc_domain_events" in sql
    assert "BEFORE UPDATE OR DELETE ON public.anc_audit_events" in sql
    assert "anc_domain_events_select_company" in sql
    assert "anc_domain_events_insert_company" in sql
    assert "anc_audit_events_select_company" in sql
    assert "anc_audit_events_insert_company" in sql


def test_budget_and_event_constraints_are_database_enforced() -> None:
    sql = MIGRATION.read_text()
    assert "spent_amount + reserved_amount <= limit_amount" in sql
    assert "UNIQUE (company_id, aggregate_type, aggregate_id, aggregate_revision)" in sql
    assert "UNIQUE (company_id, event_id)" in sql
    assert "PRIMARY KEY (company_id, work_item_id)" in sql
    assert "agent_run_id text" in sql


def test_hierarchical_budget_migration_is_additive_and_group_safe() -> None:
    sql = HIERARCHY_MIGRATION.read_text()
    assert "ADD COLUMN IF NOT EXISTS parent_budget_id" in sql
    assert "ADD COLUMN IF NOT EXISTS stage" in sql
    assert "ADD COLUMN IF NOT EXISTS reservation_group_id" in sql
    assert "ADD COLUMN IF NOT EXISTS ledger_role" in sql
    assert "anc_budgets_parent_fk" in sql
    assert "anc_budgets_active_stage_idx" in sql
    assert "anc_budget_reservations_group_role_idx" in sql
    assert "anc_budget_reservations_group_budget_idx" in sql


def test_budget_unit_migration_is_forward_compatible_with_usd_rows() -> None:
    sql = UNITS_MIGRATION.read_text()
    assert "ADD COLUMN IF NOT EXISTS unit" in sql
    assert "DEFAULT 'money'" in sql
    assert "ALTER COLUMN currency DROP NOT NULL" in sql
    assert "anc_budgets_unit_currency_check" in sql
    assert "anc_budget_reservations_unit_currency_check" in sql
    assert "time" in sql and "cycles" in sql and "tries" in sql
    assert "TRY remains Turkish lira" in sql


PARTIAL_SPEND_MIGRATION = Path(
    "supabase/migrations/202609270001_active_reservation_partial_spend.sql"
)


def test_active_reservation_partial_spend_replaces_null_actual_check() -> None:
    sql = PARTIAL_SPEND_MIGRATION.read_text()
    assert "DROP CONSTRAINT IF EXISTS anc_budget_reservations_check2" in sql
    assert "anc_budget_reservations_active_partial_spend_check" in sql
    assert "status = 'active'" in sql
    assert "actual_amount IS NULL" not in sql.split("ADD CONSTRAINT", 1)[1]

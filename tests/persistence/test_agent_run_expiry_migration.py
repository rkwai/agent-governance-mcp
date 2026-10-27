from pathlib import Path

MIGRATION = Path("supabase/migrations/202610010001_agent_run_expiry_diagnostics.sql")


def test_expiry_diagnostics_migration_names_inactive_runs_without_table_select() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION public.anc_inspect_agent_run_binding(" in sql
    assert "DROP FUNCTION IF EXISTS public.anc_resolve_agent_identity" not in sql
    assert "run_status text" in sql
    assert "instance_status text" in sql
    assert "instance.status IN ('active', 'suspended')" in sql
    assert "JOIN LATERAL" in sql
    assert "CASE WHEN candidate.status = 'running' THEN 0 ELSE 1 END" in sql
    assert "COALESCE(candidate.ended_at, candidate.updated_at) DESC" in sql
    assert "candidate.expires_at > now()" not in sql
    assert "CREATE OR REPLACE FUNCTION public.anc_list_expiring_agent_runs()" in sql
    assert "interval '72 hours'" in sql
    assert "AND run.expires_at <= now() + interval '72 hours'" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "public.anc_inspect_agent_run_binding(text, text, text, text, text)" in sql
    assert "GRANT EXECUTE ON FUNCTION public.anc_list_expiring_agent_runs()" in sql
    assert "TO ai_nature_company_runtime" in sql
    assert "FROM ai_nature_company_operator" in sql
    assert "GRANT SELECT" not in sql

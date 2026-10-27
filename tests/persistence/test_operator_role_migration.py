from pathlib import Path

MIGRATION = Path("supabase/migrations/202608300005_operator_role.sql")
OPERATOR_ROLE = "ai_nature_company_operator"

DOMAIN_APPEND_ONLY = {
    "anc_domain_events",
    "anc_audit_events",
}
DOMAIN_MUTABLE = {
    "anc_stream_heads",
    "anc_projections",
    "anc_idempotency_records",
    "anc_budgets",
    "anc_budget_reservations",
    "anc_work_leases",
}
IDENTITY_MUTABLE = {
    "anc_oauth_clients",
    "anc_auth_subjects",
    "anc_agent_instances",
    "anc_agent_runs",
}


def test_operator_role_is_login_but_has_no_privileged_or_inherited_flags() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert f"CREATE ROLE {OPERATOR_ROLE}" in sql
    for required in (
        "LOGIN",
        "NOSUPERUSER",
        "NOBYPASSRLS",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOINHERIT",
    ):
        assert required in sql
    assert "must not be a member of another role" in sql
    assert "pg_catalog.pg_auth_members" in sql


def test_operator_grants_cover_only_domain_bootstrap_and_identity_provisioning() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in DOMAIN_APPEND_ONLY | DOMAIN_MUTABLE | IDENTITY_MUTABLE:
        assert f"public.{table}" in sql
    assert "GRANT SELECT, INSERT ON TABLE" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE" in sql
    assert "GRANT DELETE" not in sql
    assert "GRANT TRUNCATE" not in sql
    assert "anc_domain_events_position_seq" in sql
    assert "anc_audit_events_position_seq" in sql


def test_operator_can_read_but_cannot_write_mcp_audit_or_resolve_tokens() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "GRANT SELECT ON TABLE public.anc_mcp_tool_calls" in sql
    assert "GRANT INSERT ON TABLE public.anc_mcp_tool_calls" not in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "anc_resolve_agent_identity(text, text, text, text, text)" in sql
    assert "has_schema_privilege" in sql
    assert "has_table_privilege" in sql
    assert "must not have Supabase auth catalog privileges" in sql

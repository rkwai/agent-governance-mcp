from pathlib import Path

MIGRATION = Path("supabase/migrations/202608300003_agent_identity_and_attribution.sql")
RUNTIME_GRANTS_MIGRATION = Path("supabase/migrations/202608300004_runtime_role_least_privilege.sql")
HIERARCHY_MIGRATION = Path("supabase/migrations/202608290002_hierarchical_budget_ledgers.sql")
TABLES = {
    "anc_oauth_clients",
    "anc_auth_subjects",
    "anc_agent_instances",
    "anc_agent_runs",
    "anc_mcp_tool_calls",
}


def _table_definition(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE IF NOT EXISTS public.{table} (")
    end = sql.index("\n);", start)
    return sql[start:end]


def test_identity_migration_defines_fixed_subject_instance_and_run_bindings() -> None:
    sql = MIGRATION.read_text()
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS public.{table}" in sql

    subjects = _table_definition(sql, "anc_auth_subjects")
    assert "UNIQUE (issuer, external_subject, oauth_client_id)" in subjects
    assert "UNIQUE (company_id, principal_id)" in subjects
    assert "REFERENCES public.anc_oauth_clients (company_id, client_id)" in subjects

    instances = _table_definition(sql, "anc_agent_instances")
    assert "definition_id text NOT NULL" in instances
    assert "definition_version integer NOT NULL" in instances
    assert "role text NOT NULL" in instances
    assert "cell_id text" in instances
    assert "UNIQUE (company_id, principal_id)" in instances
    assert "UNIQUE (company_id, definition_id)" not in instances
    assert "'founder'" not in instances

    runs = _table_definition(sql, "anc_agent_runs")
    for snapshot_field in (
        "agent_instance_id text NOT NULL",
        "principal_id text NOT NULL",
        "definition_id text NOT NULL",
        "definition_version integer NOT NULL",
        "role text NOT NULL",
    ):
        assert snapshot_field in runs
    assert "anc_agent_runs_one_running_per_instance_idx" in sql
    assert "run.expires_at > now()" in sql


def test_supabase_resolver_is_exact_session_aware_and_table_blind() -> None:
    sql = MIGRATION.read_text()
    assert "anc_resolve_agent_identity" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = public, pg_temp" in sql
    assert "to_regclass('auth.oauth_clients')" in sql
    assert "to_regclass('auth.sessions')" in sql
    assert "to_regclass('auth.refresh_tokens')" in sql
    # Supabase migration 20250903112500 removed the intermediate ``client_id``
    # column and made the UUID primary key the public OAuth client identifier.
    # Sessions and the JWT ``client_id`` claim therefore resolve through the same ID.
    assert "upstream_client.id::text = $1" in sql
    assert "upstream_client.client_id = $1" not in sql
    assert "FROM pg_catalog.pg_attribute AS attribute" in sql
    assert "attribute.atttypid = required_column.type_id" in sql
    assert "CREATE OR REPLACE FUNCTION public.anc_auth_catalog_compatible()" in sql
    assert "IF NOT public.anc_auth_catalog_compatible()" in sql
    assert "REVOKE ALL ON FUNCTION public.anc_auth_catalog_compatible() FROM PUBLIC" in sql
    for relation, column, column_type in (
        ("auth.oauth_clients", "id", "uuid"),
        ("auth.oauth_clients", "deleted_at", "timestamptz"),
        ("auth.sessions", "id", "uuid"),
        ("auth.sessions", "user_id", "uuid"),
        ("auth.sessions", "oauth_client_id", "uuid"),
        ("auth.sessions", "not_after", "timestamptz"),
        ("auth.sessions", "created_at", "timestamptz"),
        ("auth.refresh_tokens", "session_id", "uuid"),
        ("auth.refresh_tokens", "revoked", "boolean"),
    ):
        assert f"to_regclass('{relation}')" in sql
        assert f"'{column}'" in sql
        assert f"'{column_type}'::regtype" in sql
    assert "allowed_client.resource_url = $2" in sql
    assert "session.id = $5" in sql
    assert "session.user_id = $6" in sql
    assert "session.oauth_client_id = upstream_client.id" in sql
    assert "refresh.session_id = session.id" in sql
    assert "refresh.revoked = false" in sql
    assert "subject.status = 'active'" in sql
    assert "instance.status = 'active'" in sql
    assert "run.status = 'running'" in sql

    assert "REVOKE ALL ON TABLE\n    public.anc_oauth_clients" in sql
    assert "FROM ai_nature_company_runtime" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS" in sql


def test_identity_and_attribution_tables_force_company_rls() -> None:
    sql = MIGRATION.read_text()
    for table in TABLES:
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in sql
    assert "current_setting('app.company_id', true)" in sql


def test_identity_keys_and_run_attribution_are_immutable_and_revocable() -> None:
    sql = MIGRATION.read_text()
    assert "anc_oauth_clients_identity_guard" in sql
    assert "anc_auth_subjects_identity_guard" in sql
    assert "anc_agent_instances_binding_guard" in sql
    assert "anc_agent_runs_lifecycle_guard" in sql
    assert "agent principal, role, and cell binding are immutable" in sql
    assert "agent instance requires an agent authentication subject" in sql
    assert "agent run attribution and issuance fields are immutable" in sql
    assert "a revoked auth subject cannot be reactivated" in sql
    assert "token_valid_after cannot move backward" in sql
    assert "reactivation must invalidate sessions from before disablement" in sql
    assert "suspended agent runs must terminate before reactivation" in sql
    assert "a terminal agent run cannot change outcome" in sql


def test_tool_call_audit_is_append_only_two_phase_and_content_minimized() -> None:
    sql = MIGRATION.read_text()
    calls = _table_definition(sql, "anc_mcp_tool_calls")
    assert "phase IN ('received', 'succeeded', 'denied', 'failed')" in calls
    assert "UNIQUE (company_id, call_id, phase)" in calls
    assert "anc_mcp_tool_calls_one_terminal_idx" in sql
    assert "WHERE phase IN ('succeeded', 'denied', 'failed')" in sql
    assert "terminal MCP call event requires a received event" in sql
    assert "terminal MCP call attribution differs from its received event" in sql
    assert "anc_mcp_tool_calls_append_only" in sql
    assert "BEFORE UPDATE OR DELETE ON public.anc_mcp_tool_calls" in sql

    for field in (
        "principal_id",
        "authentication_method",
        "authentication_id",
        "agent_instance_id",
        "agent_run_id",
        "definition_id",
        "definition_version",
        "role",
        "tool_name",
        "arguments_sha256",
        "authorization_reason_code",
        "execution_outcome",
        "received_at",
        "recorded_at",
    ):
        assert field in calls

    # SDK/local human callers can be audited without inventing an agent or
    # persisted Supabase subject, while authenticated identity remains required.
    assert "auth_subject_id text," in calls
    assert "agent_instance_id text," in calls
    assert "role text CHECK" in calls
    assert "AND agent_run_id IS NOT NULL" in calls
    assert "principal_id text NOT NULL" in calls
    assert "authentication_id text NOT NULL" in calls

    forbidden_storage = (
        "access_token",
        "refresh_token",
        "raw_arguments",
        "arguments json",
        "prompt text",
        "response json",
        "chain_of_thought",
    )
    lowered = calls.lower()
    for forbidden in forbidden_storage:
        assert forbidden not in lowered


def test_runtime_role_grants_match_the_repository_contract_exactly() -> None:
    sql = RUNTIME_GRANTS_MIGRATION.read_text()
    append_only = {
        "anc_domain_events",
        "anc_audit_events",
        "anc_mcp_tool_calls",
    }
    mutable = {
        "anc_stream_heads",
        "anc_projections",
        "anc_idempotency_records",
        "anc_budgets",
        "anc_budget_reservations",
        "anc_work_leases",
    }
    identity = {
        "anc_oauth_clients",
        "anc_auth_subjects",
        "anc_agent_instances",
        "anc_agent_runs",
    }
    for table in append_only | mutable | identity:
        assert f"public.{table}" in sql
        assert f"'{table}'" in sql

    append_grant = sql.split("-- Append-only records:", 1)[1].split(
        "-- Deterministic projections", 1
    )[0]
    for table in append_only:
        assert f"public.{table}" in append_grant
    for table in mutable | identity:
        assert f"public.{table}" not in append_grant
    assert "GRANT SELECT, INSERT ON TABLE" in append_grant
    assert "UPDATE" not in append_grant

    mutable_grant = sql.split("-- Deterministic projections", 1)[1].split("-- Defense in depth", 1)[
        0
    ]
    for table in mutable:
        assert f"public.{table}" in mutable_grant
    for table in identity:
        assert f"public.{table}" not in mutable_grant
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE" in mutable_grant

    assert "GRANT DELETE" not in sql
    assert "GRANT UPDATE" not in append_grant
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "anc_resolve_agent_identity(text, text, text, text, text)" in sql


def test_runtime_grants_verify_role_safety_rls_and_exact_sequences() -> None:
    sql = RUNTIME_GRANTS_MIGRATION.read_text()
    for attribute in (
        "rolsuper",
        "rolbypassrls",
        "rolcreatedb",
        "rolcreaterole",
        "rolinherit",
        "relrowsecurity",
        "relforcerowsecurity",
    ):
        assert attribute in sql
    assert "REVOKE ALL ON TABLE" in sql
    assert "FROM ai_nature_company_runtime" in sql

    sequences = {
        "anc_domain_events_position_seq",
        "anc_audit_events_position_seq",
        "anc_mcp_tool_calls_position_seq",
    }
    for sequence in sequences:
        assert f"public.{sequence}" in sql
    assert "GRANT USAGE, SELECT ON SEQUENCE" in sql


def test_hierarchical_budget_migration_adds_columns_not_tables() -> None:
    sql = HIERARCHY_MIGRATION.read_text()
    assert "CREATE TABLE" not in sql
    assert "ALTER TABLE public.anc_budgets" in sql
    assert "ALTER TABLE public.anc_budget_reservations" in sql

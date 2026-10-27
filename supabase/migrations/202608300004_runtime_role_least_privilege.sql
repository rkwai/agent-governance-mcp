-- Least-privilege production grants for PostgresRepository and MCP attribution.
-- Identity resolution remains available only through the hardened resolver.

DO $runtime_role_safety$
DECLARE
    runtime_role record;
    table_name text;
BEGIN
    SELECT * INTO runtime_role
    FROM pg_roles
    WHERE rolname = 'ai_nature_company_runtime';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ai_nature_company_runtime must exist before applying runtime grants';
    END IF;
    IF runtime_role.rolsuper
       OR runtime_role.rolbypassrls
       OR runtime_role.rolcreatedb
       OR runtime_role.rolcreaterole
       OR runtime_role.rolinherit THEN
        RAISE EXCEPTION
            'ai_nature_company_runtime must be NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, and NOINHERIT';
    END IF;

    FOREACH table_name IN ARRAY ARRAY[
        'anc_stream_heads',
        'anc_domain_events',
        'anc_audit_events',
        'anc_projections',
        'anc_idempotency_records',
        'anc_budgets',
        'anc_budget_reservations',
        'anc_work_leases',
        'anc_oauth_clients',
        'anc_auth_subjects',
        'anc_agent_instances',
        'anc_agent_runs',
        'anc_mcp_tool_calls'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = table_name
              AND relation.relkind IN ('r', 'p')
              AND relation.relrowsecurity
              AND relation.relforcerowsecurity
        ) THEN
            RAISE EXCEPTION 'public.% must enable and force row-level security', table_name;
        END IF;
    END LOOP;
END;
$runtime_role_safety$;

GRANT USAGE ON SCHEMA public TO ai_nature_company_runtime;

-- Reset the governed surface before granting the exact repository contract.
REVOKE ALL ON TABLE
    public.anc_stream_heads,
    public.anc_domain_events,
    public.anc_audit_events,
    public.anc_projections,
    public.anc_idempotency_records,
    public.anc_budgets,
    public.anc_budget_reservations,
    public.anc_work_leases,
    public.anc_oauth_clients,
    public.anc_auth_subjects,
    public.anc_agent_instances,
    public.anc_agent_runs,
    public.anc_mcp_tool_calls
    FROM ai_nature_company_runtime;

REVOKE ALL ON SEQUENCE
    public.anc_domain_events_position_seq,
    public.anc_audit_events_position_seq,
    public.anc_mcp_tool_calls_position_seq
    FROM ai_nature_company_runtime;

-- Append-only records: read and append, never mutate or delete.
GRANT SELECT, INSERT ON TABLE
    public.anc_domain_events,
    public.anc_audit_events,
    public.anc_mcp_tool_calls
    TO ai_nature_company_runtime;

-- Deterministic projections and coordination records: no hard deletion.
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.anc_stream_heads,
    public.anc_projections,
    public.anc_idempotency_records,
    public.anc_budgets,
    public.anc_budget_reservations,
    public.anc_work_leases
    TO ai_nature_company_runtime;

GRANT USAGE, SELECT ON SEQUENCE
    public.anc_domain_events_position_seq,
    public.anc_audit_events_position_seq,
    public.anc_mcp_tool_calls_position_seq
    TO ai_nature_company_runtime;

-- Defense in depth: the application login cannot enumerate or mutate identity,
-- agent-instance, or run authority directly.
REVOKE ALL ON TABLE
    public.anc_oauth_clients,
    public.anc_auth_subjects,
    public.anc_agent_instances,
    public.anc_agent_runs
    FROM ai_nature_company_runtime;

GRANT EXECUTE ON FUNCTION
    public.anc_resolve_agent_identity(text, text, text, text, text)
    TO ai_nature_company_runtime;
GRANT EXECUTE ON FUNCTION public.anc_auth_catalog_compatible()
    TO ai_nature_company_runtime;

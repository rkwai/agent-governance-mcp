-- One offline operator identity for domain bootstrap and agent-identity provisioning.
--
-- This role is deliberately distinct from the hosted runtime.  The migration never
-- assigns a password: an operator must set or rotate it through the deployment's
-- secret-management path after migrations have completed.

DO $operator_role_safety$
DECLARE
    operator_role record;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'ai_nature_company_operator'
    ) THEN
        CREATE ROLE ai_nature_company_operator
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOREPLICATION
            NOBYPASSRLS;
    END IF;

    SELECT * INTO operator_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'ai_nature_company_operator';

    IF operator_role.rolsuper
       OR operator_role.rolbypassrls
       OR operator_role.rolcreatedb
       OR operator_role.rolcreaterole
       OR operator_role.rolreplication
       OR operator_role.rolinherit
       OR NOT operator_role.rolcanlogin THEN
        RAISE EXCEPTION
            'ai_nature_company_operator must be LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOREPLICATION, and NOINHERIT';
    END IF;

    -- NOINHERIT is not sufficient: a member can still SET ROLE explicitly.  The
    -- operator must not be able to assume service_role or any other role.
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE member_role.rolname = 'ai_nature_company_operator'
    ) THEN
        RAISE EXCEPTION 'ai_nature_company_operator must not be a member of another role';
    END IF;
END;
$operator_role_safety$;

GRANT USAGE ON SCHEMA public TO ai_nature_company_operator;

-- Reset the entire governed surface before granting the exact offline contract.
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
    FROM ai_nature_company_operator;

REVOKE ALL ON SEQUENCE
    public.anc_domain_events_position_seq,
    public.anc_audit_events_position_seq,
    public.anc_mcp_tool_calls_position_seq
    FROM ai_nature_company_operator;

REVOKE ALL ON FUNCTION
    public.anc_resolve_agent_identity(text, text, text, text, text)
    FROM ai_nature_company_operator;
REVOKE ALL ON FUNCTION public.anc_auth_catalog_compatible()
    FROM ai_nature_company_operator;

-- Domain history is append-only.  The operator can build canonical state only by
-- calling the same repository/service path as the hosted runtime.
GRANT SELECT, INSERT ON TABLE
    public.anc_domain_events,
    public.anc_audit_events
    TO ai_nature_company_operator;

GRANT SELECT, INSERT, UPDATE ON TABLE
    public.anc_stream_heads,
    public.anc_projections,
    public.anc_idempotency_records,
    public.anc_budgets,
    public.anc_budget_reservations,
    public.anc_work_leases
    TO ai_nature_company_operator;

GRANT USAGE, SELECT ON SEQUENCE
    public.anc_domain_events_position_seq,
    public.anc_audit_events_position_seq
    TO ai_nature_company_operator;

-- Identity records activate only after the provisioning script verifies matching
-- domain projections.  They are mutable for revocation and rotation, never deletable.
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.anc_oauth_clients,
    public.anc_auth_subjects,
    public.anc_agent_instances,
    public.anc_agent_runs
    TO ai_nature_company_operator;

-- The offline operator may inspect attribution for beta verification and incident
-- response, but cannot append, mutate, or delete hosted call records.
GRANT SELECT ON TABLE public.anc_mcp_tool_calls TO ai_nature_company_operator;
REVOKE ALL ON SEQUENCE public.anc_mcp_tool_calls_position_seq
    FROM ai_nature_company_operator;
REVOKE ALL ON FUNCTION
    public.anc_resolve_agent_identity(text, text, text, text, text)
    FROM ai_nature_company_operator;
REVOKE ALL ON FUNCTION public.anc_auth_catalog_compatible()
    FROM ai_nature_company_operator;

-- Supabase's auth catalog is outside this application's provisioning contract.
-- Validate rather than changing privileges owned by Supabase-managed roles.
DO $operator_auth_catalog$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'auth'
    ) THEN
        IF has_schema_privilege(
            'ai_nature_company_operator', 'auth', 'USAGE'
        ) OR has_schema_privilege(
            'ai_nature_company_operator', 'auth', 'CREATE'
        ) THEN
            RAISE EXCEPTION
                'ai_nature_company_operator must not have Supabase auth schema access';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'auth'
              AND relation.relkind IN ('r', 'p', 'v', 'm')
              AND has_table_privilege(
                  'ai_nature_company_operator', relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
        ) THEN
            RAISE EXCEPTION
                'ai_nature_company_operator must not have Supabase auth catalog privileges';
        END IF;
    END IF;
END;
$operator_auth_catalog$;

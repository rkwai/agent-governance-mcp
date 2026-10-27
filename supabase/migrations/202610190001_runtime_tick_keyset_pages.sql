-- Keyset-paginated reads for the report-only runtime tick.
--
-- 202610180001 shipped single-call reads capped at 100 companies and 200 runs,
-- which always returned the same first rows and silently skipped the rest.
-- These page functions let the tick walk every company and every running run.
-- The tick loops until a short page and marks a section truncated only if its
-- own safety cap is reached.
--
-- New names (not overloads) so /ready can fingerprint them by name.  The
-- 202610180001 functions stay in place until a later cleanup migration after
-- this code is live; the previous deploy does not call these.

CREATE OR REPLACE FUNCTION public.anc_list_tick_company_ids_page(
    p_after_company_id text,
    p_limit integer
)
RETURNS TABLE (company_id text)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public, pg_temp
AS $function$
    SELECT projection.company_id
    FROM public.anc_projections AS projection
    WHERE projection.projection_type = 'company'
      AND (p_after_company_id IS NULL OR projection.company_id > p_after_company_id)
    ORDER BY projection.company_id ASC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 100), 1), 500);
$function$;

REVOKE ALL ON FUNCTION public.anc_list_tick_company_ids_page(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.anc_list_tick_company_ids_page(text, integer)
    FROM ai_nature_company_operator;
GRANT EXECUTE ON FUNCTION public.anc_list_tick_company_ids_page(text, integer)
    TO ai_nature_company_runtime;

CREATE OR REPLACE FUNCTION public.anc_list_maintenance_agent_runs_page(
    p_company_id text,
    p_after_expires_at timestamptz,
    p_after_agent_run_id text,
    p_limit integer
)
RETURNS TABLE (
    agent_run_id text,
    agent_instance_id text,
    role text,
    cell_id text,
    expires_at timestamptz,
    instance_status text
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public, pg_temp
AS $function$
    SELECT
        run.agent_run_id,
        run.agent_instance_id,
        run.role,
        run.cell_id,
        run.expires_at,
        instance.status
    FROM public.anc_agent_runs AS run
    JOIN public.anc_agent_instances AS instance
      ON instance.company_id = run.company_id
     AND instance.agent_instance_id = run.agent_instance_id
    WHERE run.company_id = p_company_id
      AND run.status = 'running'
      AND run.started_at <= now()
      AND (
        p_after_expires_at IS NULL
        OR (run.expires_at, run.agent_run_id)
            > (p_after_expires_at, COALESCE(p_after_agent_run_id, ''))
      )
    ORDER BY run.expires_at ASC, run.agent_run_id ASC
    LIMIT LEAST(GREATEST(COALESCE(p_limit, 200), 1), 500);
$function$;

REVOKE ALL ON FUNCTION
    public.anc_list_maintenance_agent_runs_page(text, timestamptz, text, integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.anc_list_maintenance_agent_runs_page(text, timestamptz, text, integer)
    FROM ai_nature_company_operator;
GRANT EXECUTE ON FUNCTION
    public.anc_list_maintenance_agent_runs_page(text, timestamptz, text, integer)
    TO ai_nature_company_runtime;

COMMENT ON FUNCTION public.anc_list_tick_company_ids_page(text, integer) IS
    'Keyset page of company ids for the report-only runtime tick; ids only.';
COMMENT ON FUNCTION
    public.anc_list_maintenance_agent_runs_page(text, timestamptz, text, integer) IS
    'Keyset page over (expires_at, agent_run_id) of running agent runs for one company; read by the report-only runtime tick.';

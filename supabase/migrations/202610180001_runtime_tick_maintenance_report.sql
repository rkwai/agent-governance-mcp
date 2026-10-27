-- Report-only runtime tick (Maintain/Continue) reads.
--
-- The in-process tick scheduler writes a maintenance report and makes zero
-- domain state mutations.  It needs two content-minimized reads the runtime
-- role cannot do directly under forced RLS:
--
--   * which companies exist (to tick each one), and
--   * which running agent runs belong to a company, including runs whose
--     expires_at already passed while status is still 'running'.
--
-- Both are SECURITY DEFINER, STABLE, return no secrets or subjects, and are
-- executable only by ai_nature_company_runtime.  Apply in production before or
-- with the Railway deploy; /ready fingerprints both functions.

CREATE OR REPLACE FUNCTION public.anc_list_tick_company_ids()
RETURNS TABLE (company_id text)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public, pg_temp
AS $function$
    SELECT projection.company_id
    FROM public.anc_projections AS projection
    WHERE projection.projection_type = 'company'
    ORDER BY projection.company_id ASC
    LIMIT 100;
$function$;

REVOKE ALL ON FUNCTION public.anc_list_tick_company_ids() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.anc_list_tick_company_ids()
    FROM ai_nature_company_operator;
GRANT EXECUTE ON FUNCTION public.anc_list_tick_company_ids()
    TO ai_nature_company_runtime;

CREATE OR REPLACE FUNCTION public.anc_list_maintenance_agent_runs(p_company_id text)
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
    ORDER BY run.expires_at ASC, run.agent_run_id ASC
    LIMIT 200;
$function$;

REVOKE ALL ON FUNCTION public.anc_list_maintenance_agent_runs(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.anc_list_maintenance_agent_runs(text)
    FROM ai_nature_company_operator;
GRANT EXECUTE ON FUNCTION public.anc_list_maintenance_agent_runs(text)
    TO ai_nature_company_runtime;

COMMENT ON FUNCTION public.anc_list_tick_company_ids() IS
    'Company ids the report-only runtime tick should inspect; ids only.';
COMMENT ON FUNCTION public.anc_list_maintenance_agent_runs(text) IS
    'Content-minimized running agent runs for one company, including already-expired running runs; read by the report-only runtime tick.';

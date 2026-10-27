-- Killing a cell ends its running agent runs in the same transaction.
--
-- The runtime role has no direct UPDATE on anc_agent_runs (identity and run
-- authority stay behind SECURITY DEFINER functions), so cell.transition and
-- maintenance.run call this narrowly scoped function.  It only moves
-- status='running' rows of one company cell to a terminal status; the
-- anc_protect_agent_run trigger still enforces immutability, next revision,
-- and monotonic time.

CREATE OR REPLACE FUNCTION public.anc_end_cell_agent_runs(
    p_company_id text,
    p_cell_id text,
    p_termination_code text,
    p_ended_at timestamptz
)
RETURNS TABLE (agent_run_id text)
LANGUAGE plpgsql
SECURITY DEFINER
VOLATILE
SET search_path = public, pg_temp
AS $function$
BEGIN
    IF p_company_id IS NULL OR p_cell_id IS NULL OR p_ended_at IS NULL THEN
        RAISE EXCEPTION 'company, cell, and ended_at are required' USING ERRCODE = '22004';
    END IF;
    IF p_termination_code IS NULL OR char_length(p_termination_code) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'termination_code must be 1..128 characters' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    UPDATE public.anc_agent_runs AS run
    SET status = 'revoked',
        termination_code = p_termination_code,
        ended_at = GREATEST(p_ended_at, run.started_at),
        revision = run.revision + 1,
        updated_at = GREATEST(p_ended_at, run.updated_at)
    WHERE run.company_id = p_company_id
      AND run.cell_id = p_cell_id
      AND run.status = 'running'
    RETURNING run.agent_run_id;
END;
$function$;

REVOKE ALL ON FUNCTION public.anc_end_cell_agent_runs(text, text, text, timestamptz)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION public.anc_end_cell_agent_runs(text, text, text, timestamptz)
    FROM ai_nature_company_operator;
GRANT EXECUTE ON FUNCTION public.anc_end_cell_agent_runs(text, text, text, timestamptz)
    TO ai_nature_company_runtime;

COMMENT ON FUNCTION public.anc_end_cell_agent_runs(text, text, text, timestamptz) IS
    'Runtime-only: end every running agent run of one cell (cell kill / maintenance.run).';

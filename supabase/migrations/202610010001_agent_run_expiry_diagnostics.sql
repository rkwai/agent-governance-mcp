-- Diagnose caller-owned expired, revoked, or suspended agent runs instead of
-- collapsing them into a generic unauthenticated miss.  Also let the runtime
-- list running runs that expire within 72 hours without SELECT on identity
-- tables.  Apply by hand in production before or with the Railway deploy.
--
-- Do not change the return type of anc_resolve_agent_identity: later tests and
-- historical CREATE OR REPLACE migrations reuse that exact OUT row type.

CREATE OR REPLACE FUNCTION public.anc_inspect_agent_run_binding(
    p_issuer text,
    p_subject text,
    p_client_id text,
    p_resource_url text,
    p_session_id text
)
RETURNS TABLE (
    company_id text,
    auth_subject_id text,
    principal_id text,
    subject_kind text,
    token_valid_after timestamptz,
    agent_instance_id text,
    definition_id text,
    definition_version integer,
    assigned_role text,
    cell_id text,
    agent_run_id text,
    run_expires_at timestamptz,
    run_status text,
    instance_status text
)
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = public, pg_temp
AS $function$
DECLARE
    verified_subject uuid;
    verified_session uuid;
BEGIN
    IF NOT public.anc_auth_catalog_compatible() THEN
        RETURN;
    END IF;
    BEGIN
        verified_subject := p_subject::uuid;
        verified_session := p_session_id::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN;
    END;

    RETURN QUERY EXECUTE $inspector_query$
        SELECT
            subject.company_id,
            subject.auth_subject_id,
            subject.principal_id,
            subject.subject_kind,
            subject.token_valid_after,
            instance.agent_instance_id,
            instance.definition_id,
            instance.definition_version,
            instance.role,
            instance.cell_id,
            run.agent_run_id,
            run.expires_at,
            run.status,
            instance.status
        FROM public.anc_auth_subjects AS subject
        JOIN public.anc_oauth_clients AS allowed_client
          ON allowed_client.company_id = subject.company_id
         AND allowed_client.client_id = subject.oauth_client_id
         AND allowed_client.client_id = $1
         AND allowed_client.status = 'active'
         AND (
            allowed_client.resource_url = $2
            OR EXISTS (
                SELECT 1
                FROM public.anc_oauth_client_resource_aliases AS allowed_alias
                WHERE allowed_alias.company_id = allowed_client.company_id
                  AND allowed_alias.client_id = allowed_client.client_id
                  AND allowed_alias.resource_url = $2
                  AND allowed_alias.status = 'active'
            )
         )
        JOIN auth.oauth_clients AS upstream_client
          ON upstream_client.id::text = $1
         AND upstream_client.deleted_at IS NULL
        JOIN auth.sessions AS session
          ON session.id = $5
         AND session.user_id = $6
         AND session.oauth_client_id = upstream_client.id
         AND (session.not_after IS NULL OR session.not_after > now())
         AND (
            subject.token_valid_after IS NULL
            OR session.created_at >= subject.token_valid_after
         )
        JOIN public.anc_agent_instances AS instance
          ON instance.company_id = subject.company_id
         AND instance.principal_id = subject.principal_id
         AND instance.status IN ('active', 'suspended')
        JOIN LATERAL (
            SELECT
                candidate.agent_run_id,
                candidate.expires_at,
                candidate.status
            FROM public.anc_agent_runs AS candidate
            WHERE candidate.company_id = instance.company_id
              AND candidate.agent_instance_id = instance.agent_instance_id
              AND candidate.principal_id = instance.principal_id
              AND candidate.definition_id = instance.definition_id
              AND candidate.definition_version = instance.definition_version
              AND candidate.role = instance.role
              AND candidate.cell_id IS NOT DISTINCT FROM instance.cell_id
              AND candidate.started_at <= now()
            ORDER BY
              CASE WHEN candidate.status = 'running' THEN 0 ELSE 1 END,
              COALESCE(candidate.ended_at, candidate.updated_at) DESC,
              candidate.started_at DESC,
              candidate.agent_run_id DESC
            LIMIT 1
        ) AS run ON true
        WHERE subject.issuer = $3
          AND subject.external_subject = $4
          AND subject.oauth_client_id = $1
          AND subject.subject_kind = 'agent'
          AND subject.status = 'active'
          AND EXISTS (
            SELECT 1
            FROM auth.refresh_tokens AS refresh
            WHERE refresh.session_id = session.id
              AND refresh.revoked = false
          )
    $inspector_query$
    USING
        p_client_id,
        p_resource_url,
        p_issuer,
        p_subject,
        verified_session,
        verified_subject;
END;
$function$;

REVOKE ALL ON FUNCTION
    public.anc_inspect_agent_run_binding(text, text, text, text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.anc_inspect_agent_run_binding(text, text, text, text, text)
    FROM ai_nature_company_operator;
GRANT EXECUTE ON FUNCTION
    public.anc_inspect_agent_run_binding(text, text, text, text, text)
    TO ai_nature_company_runtime;

CREATE OR REPLACE FUNCTION public.anc_list_expiring_agent_runs()
RETURNS TABLE (
    agent_run_id text,
    agent_instance_id text,
    expires_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public, pg_temp
AS $function$
    SELECT run.agent_run_id, run.agent_instance_id, run.expires_at
    FROM public.anc_agent_runs AS run
    WHERE run.status = 'running'
      AND run.started_at <= now()
      AND run.expires_at > now()
      AND run.expires_at <= now() + interval '72 hours'
    ORDER BY run.expires_at ASC, run.agent_run_id ASC
    LIMIT 50;
$function$;

REVOKE ALL ON FUNCTION public.anc_list_expiring_agent_runs() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.anc_list_expiring_agent_runs()
    FROM ai_nature_company_operator;
GRANT EXECUTE ON FUNCTION public.anc_list_expiring_agent_runs()
    TO ai_nature_company_runtime;

COMMENT ON FUNCTION public.anc_inspect_agent_run_binding(text, text, text, text, text) IS
    'Caller-owned subject binding plus inactive run or instance status; used only after anc_resolve_agent_identity returns no live run.';
COMMENT ON FUNCTION public.anc_list_expiring_agent_runs() IS
    'Content-minimized list of running agent runs that expire within 72 hours; used for operator warnings and must not affect readiness.';

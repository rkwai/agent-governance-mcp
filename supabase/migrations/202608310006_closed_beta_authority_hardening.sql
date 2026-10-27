-- Closed-beta authority hardening.  Additive only: do not edit 001-005.
--
-- Closes the pre-deployment P1/P2 findings that remain after the identity
-- boundary merge: resolver start-time, attribution EXECUTE ACL, runtime-role
-- drift, persisted attribution/subject/cell-owner invariants.

-- Defense in depth: a future started_at must not resolve before its window.
CREATE OR REPLACE FUNCTION public.anc_resolve_agent_identity(
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
    run_expires_at timestamptz
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

    RETURN QUERY EXECUTE $resolver_query$
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
            run.expires_at
        FROM public.anc_auth_subjects AS subject
        JOIN public.anc_oauth_clients AS allowed_client
          ON allowed_client.company_id = subject.company_id
         AND allowed_client.client_id = subject.oauth_client_id
         AND allowed_client.client_id = $1
         AND allowed_client.resource_url = $2
         AND allowed_client.status = 'active'
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
         AND instance.status = 'active'
        JOIN public.anc_agent_runs AS run
          ON run.company_id = instance.company_id
         AND run.agent_instance_id = instance.agent_instance_id
         AND run.principal_id = instance.principal_id
         AND run.definition_id = instance.definition_id
         AND run.definition_version = instance.definition_version
         AND run.role = instance.role
         AND run.cell_id IS NOT DISTINCT FROM instance.cell_id
         AND run.status = 'running'
         AND run.started_at <= now()
         AND run.expires_at > now()
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
    $resolver_query$
    USING
        p_client_id,
        p_resource_url,
        p_issuer,
        p_subject,
        verified_session,
        verified_subject;
END;
$function$;

REVOKE ALL ON FUNCTION public.anc_resolve_agent_identity(text, text, text, text, text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    public.anc_resolve_agent_identity(text, text, text, text, text)
    TO ai_nature_company_runtime;

-- Terminal rows must carry the same trusted role and cell as their received row,
-- including local/non-agent calls that have no instance binding.
CREATE OR REPLACE FUNCTION public.anc_validate_mcp_tool_call_attribution()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
    instance_record public.anc_agent_instances%ROWTYPE;
    run_record public.anc_agent_runs%ROWTYPE;
    received_record public.anc_mcp_tool_calls%ROWTYPE;
BEGIN
    IF NEW.agent_instance_id IS NOT NULL THEN
        SELECT * INTO instance_record
        FROM public.anc_agent_instances AS instance
        WHERE instance.company_id = NEW.company_id
          AND instance.agent_instance_id = NEW.agent_instance_id
          AND instance.principal_id = NEW.principal_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'MCP call agent instance attribution is unknown'
                USING ERRCODE = '23503';
        END IF;
        IF ROW(NEW.role, NEW.cell_id) IS DISTINCT FROM
           ROW(instance_record.role, instance_record.cell_id) THEN
            RAISE EXCEPTION 'MCP call role or cell does not match its agent binding'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.agent_run_id IS NOT NULL THEN
        SELECT * INTO run_record
        FROM public.anc_agent_runs AS run
        WHERE run.company_id = NEW.company_id
          AND run.agent_run_id = NEW.agent_run_id
          AND run.agent_instance_id = NEW.agent_instance_id
          AND run.principal_id = NEW.principal_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'MCP call agent run attribution is unknown'
                USING ERRCODE = '23503';
        END IF;
        IF ROW(
            NEW.definition_id, NEW.definition_version, NEW.role, NEW.cell_id
        ) IS DISTINCT FROM ROW(
            run_record.definition_id, run_record.definition_version,
            run_record.role, run_record.cell_id
        ) THEN
            RAISE EXCEPTION 'MCP call attribution does not match its agent run'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.agent_instance_id IS NOT NULL AND ROW(
        NEW.definition_id, NEW.definition_version
    ) IS DISTINCT FROM ROW(
        instance_record.definition_id, instance_record.definition_version
    ) THEN
        RAISE EXCEPTION 'MCP call definition does not match its agent binding'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.phase <> 'received' THEN
        SELECT * INTO received_record
        FROM public.anc_mcp_tool_calls AS call
        WHERE call.company_id = NEW.company_id
          AND call.call_id = NEW.call_id
          AND call.phase = 'received';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'terminal MCP call event requires a received event'
                USING ERRCODE = '23503';
        END IF;
        IF ROW(
            NEW.request_id, NEW.correlation_id, NEW.principal_id,
            NEW.auth_subject_id, NEW.authentication_method, NEW.authentication_id,
            NEW.agent_instance_id, NEW.agent_run_id, NEW.definition_id,
            NEW.definition_version, NEW.role, NEW.cell_id, NEW.tool_name,
            NEW.transport, NEW.protocol_version, NEW.arguments_sha256,
            NEW.idempotency_key_sha256, NEW.received_at
        ) IS DISTINCT FROM ROW(
            received_record.request_id, received_record.correlation_id,
            received_record.principal_id, received_record.auth_subject_id,
            received_record.authentication_method, received_record.authentication_id,
            received_record.agent_instance_id, received_record.agent_run_id,
            received_record.definition_id, received_record.definition_version,
            received_record.role, received_record.cell_id,
            received_record.tool_name,
            received_record.transport, received_record.protocol_version,
            received_record.arguments_sha256, received_record.idempotency_key_sha256,
            received_record.received_at
        ) THEN
            RAISE EXCEPTION 'terminal MCP call attribution differs from its received event'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION public.anc_validate_mcp_tool_call_attribution() FROM PUBLIC;

-- An active instance or running run cannot be created for a disabled/revoked subject.
CREATE OR REPLACE FUNCTION public.anc_validate_agent_instance_subject()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.anc_auth_subjects AS subject
        WHERE subject.company_id = NEW.company_id
          AND subject.principal_id = NEW.principal_id
          AND subject.subject_kind = 'agent'
    ) THEN
        RAISE EXCEPTION 'agent instance requires an agent authentication subject'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'active' AND EXISTS (
        SELECT 1
        FROM public.anc_auth_subjects AS subject
        WHERE subject.company_id = NEW.company_id
          AND subject.principal_id = NEW.principal_id
          AND subject.status IN ('disabled', 'revoked')
    ) THEN
        RAISE EXCEPTION 'an active agent instance cannot be created for a disabled or revoked subject'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS anc_agent_instances_subject_guard ON public.anc_agent_instances;
CREATE TRIGGER anc_agent_instances_subject_guard
BEFORE INSERT OR UPDATE ON public.anc_agent_instances
FOR EACH ROW EXECUTE FUNCTION public.anc_validate_agent_instance_subject();

CREATE OR REPLACE FUNCTION public.anc_protect_agent_run()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    instance_record public.anc_agent_instances%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO instance_record
        FROM public.anc_agent_instances AS instance
        WHERE instance.company_id = NEW.company_id
          AND instance.agent_instance_id = NEW.agent_instance_id
          AND instance.principal_id = NEW.principal_id
          AND instance.status = 'active';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'agent run requires an active matching instance'
                USING ERRCODE = '23503';
        END IF;
        IF ROW(
            NEW.definition_id, NEW.definition_version, NEW.role, NEW.cell_id
        ) IS DISTINCT FROM ROW(
            instance_record.definition_id, instance_record.definition_version,
            instance_record.role, instance_record.cell_id
        ) THEN
            RAISE EXCEPTION 'agent run attribution does not match its instance binding'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.status = 'running' AND EXISTS (
            SELECT 1
            FROM public.anc_auth_subjects AS subject
            WHERE subject.company_id = NEW.company_id
              AND subject.principal_id = NEW.principal_id
              AND subject.status IN ('disabled', 'revoked')
        ) THEN
            RAISE EXCEPTION 'a running agent run cannot be created for a disabled or revoked subject'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent run history is retained; DELETE is forbidden'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.company_id, OLD.agent_run_id, OLD.agent_instance_id,
        OLD.principal_id, OLD.definition_id, OLD.definition_version,
        OLD.role, OLD.cell_id, OLD.started_at, OLD.expires_at, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.company_id, NEW.agent_run_id, NEW.agent_instance_id,
        NEW.principal_id, NEW.definition_id, NEW.definition_version,
        NEW.role, NEW.cell_id, NEW.started_at, NEW.expires_at, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'agent run attribution and issuance fields are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'running' AND ROW(
        NEW.status, NEW.ended_at, NEW.termination_code
    ) IS DISTINCT FROM ROW(
        OLD.status, OLD.ended_at, OLD.termination_code
    ) THEN
        RAISE EXCEPTION 'a terminal agent run cannot change outcome'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.revision <> OLD.revision + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'agent run updates require the next revision and monotonic time'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$function$;

-- Active cell_owner instances must be bound to a cell in the authority schema.
ALTER TABLE public.anc_agent_instances
    DROP CONSTRAINT IF EXISTS anc_agent_instances_active_cell_owner_requires_cell;
ALTER TABLE public.anc_agent_instances
    ADD CONSTRAINT anc_agent_instances_active_cell_owner_requires_cell
    CHECK (NOT (role = 'cell_owner' AND status = 'active' AND cell_id IS NULL))
    NOT VALID;
ALTER TABLE public.anc_agent_instances
    VALIDATE CONSTRAINT anc_agent_instances_active_cell_owner_requires_cell;

-- Runtime role: reject replication, memberships, and public-schema CREATE.
DO $runtime_role_hardening$
DECLARE
    runtime_role record;
BEGIN
    SELECT * INTO runtime_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'ai_nature_company_runtime';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ai_nature_company_runtime must exist before authority hardening';
    END IF;
    IF runtime_role.rolsuper
       OR runtime_role.rolbypassrls
       OR runtime_role.rolcreatedb
       OR runtime_role.rolcreaterole
       OR runtime_role.rolinherit
       OR runtime_role.rolreplication THEN
        RAISE EXCEPTION
            'ai_nature_company_runtime must be NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOINHERIT, and NOREPLICATION';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = runtime_role.oid
    ) THEN
        RAISE EXCEPTION 'ai_nature_company_runtime must not be a member of another role';
    END IF;
    IF has_schema_privilege('ai_nature_company_runtime', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'ai_nature_company_runtime must not CREATE objects in public';
    END IF;
END;
$runtime_role_hardening$;

-- Additional public MCP resource URLs for one existing OAuth client.
-- Additive only: do not edit 001-006.
--
-- Grok Bot stores one authorization per connector URL.  Distinct Railway
-- services therefore advertise distinct https://<host>/mcp resources while
-- remaining aliases of one allowlisted client.  Each hosted process still
-- has one ANC_PUBLIC_MCP_URL; this table does not introduce a path router.
-- Exact-match semantics are preserved: a token resource must equal the
-- client's primary resource_url or an active alias of that same client.

CREATE TABLE IF NOT EXISTS public.anc_oauth_client_resource_aliases (
    company_id text NOT NULL,
    client_id text NOT NULL,
    resource_url text NOT NULL CHECK (resource_url ~ '^https://[^?#]+/mcp$'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, client_id, resource_url),
    FOREIGN KEY (company_id, client_id)
        REFERENCES public.anc_oauth_clients (company_id, client_id)
        ON DELETE RESTRICT,
    CHECK (char_length(company_id) BETWEEN 1 AND 256),
    CHECK (
        octet_length(client_id) BETWEEN 1 AND 256
        AND client_id = btrim(client_id)
    ),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS anc_oauth_client_resource_aliases_lookup_idx
    ON public.anc_oauth_client_resource_aliases
        (company_id, client_id, status, resource_url);

INSERT INTO public.anc_oauth_client_resource_aliases (
    company_id, client_id, resource_url, status, revision, created_at, updated_at
)
SELECT
    company_id,
    client_id,
    resource_url,
    'active',
    0,
    created_at,
    created_at
FROM public.anc_oauth_clients
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION public.anc_protect_oauth_client_resource_alias()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'OAuth client resource alias history is retained; DELETE is forbidden'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.company_id, OLD.client_id, OLD.resource_url, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.company_id, NEW.client_id, NEW.resource_url, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'OAuth client resource alias identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.revision <> OLD.revision + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION
            'OAuth client resource alias updates require the next revision and monotonic time'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS anc_oauth_client_resource_aliases_identity_guard
    ON public.anc_oauth_client_resource_aliases;
CREATE TRIGGER anc_oauth_client_resource_aliases_identity_guard
BEFORE UPDATE OR DELETE ON public.anc_oauth_client_resource_aliases
FOR EACH ROW EXECUTE FUNCTION public.anc_protect_oauth_client_resource_alias();

ALTER TABLE public.anc_oauth_client_resource_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_oauth_client_resource_aliases FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS anc_oauth_client_resource_aliases_select_company
    ON public.anc_oauth_client_resource_aliases;
CREATE POLICY anc_oauth_client_resource_aliases_select_company
    ON public.anc_oauth_client_resource_aliases
FOR SELECT
USING (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_oauth_client_resource_aliases_insert_company
    ON public.anc_oauth_client_resource_aliases;
CREATE POLICY anc_oauth_client_resource_aliases_insert_company
    ON public.anc_oauth_client_resource_aliases
FOR INSERT
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_oauth_client_resource_aliases_update_company
    ON public.anc_oauth_client_resource_aliases;
CREATE POLICY anc_oauth_client_resource_aliases_update_company
    ON public.anc_oauth_client_resource_aliases
FOR UPDATE
USING (company_id = NULLIF(current_setting('app.company_id', true), ''))
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

-- Keep issuer, audience, client, subject, session, and token-time checks.
-- Accept the client's immutable primary resource_url or an active alias.
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

REVOKE ALL ON FUNCTION public.anc_protect_oauth_client_resource_alias() FROM PUBLIC;
REVOKE ALL ON TABLE public.anc_oauth_client_resource_aliases FROM PUBLIC;
REVOKE ALL ON TABLE public.anc_oauth_client_resource_aliases
    FROM ai_nature_company_runtime;
REVOKE ALL ON TABLE public.anc_oauth_client_resource_aliases
    FROM ai_nature_company_operator;

GRANT SELECT, INSERT, UPDATE ON TABLE public.anc_oauth_client_resource_aliases
    TO ai_nature_company_operator;

COMMENT ON TABLE public.anc_oauth_client_resource_aliases IS
    'Additional exact protected-MCP-resource URLs for one allowlisted OAuth client; the client primary resource_url remains valid.';
COMMENT ON TABLE public.anc_oauth_clients IS
    'Exact Supabase OAuth client allowlist; resource_url is the immutable primary protected MCP resource and may be extended by aliases.';

DO $alias_surface_safety$
DECLARE
    runtime_role record;
BEGIN
    SELECT * INTO runtime_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'ai_nature_company_runtime';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ai_nature_company_runtime must exist before resource aliases';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relname = 'anc_oauth_client_resource_aliases'
          AND relation.relkind IN ('r', 'p')
          AND relation.relrowsecurity
          AND relation.relforcerowsecurity
    ) THEN
        RAISE EXCEPTION
            'public.anc_oauth_client_resource_aliases must enable and force row-level security';
    END IF;
    IF has_table_privilege(
        'ai_nature_company_runtime',
        'public.anc_oauth_client_resource_aliases',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
    ) THEN
        RAISE EXCEPTION
            'ai_nature_company_runtime must not have privileges on resource aliases';
    END IF;
END;
$alias_surface_safety$;

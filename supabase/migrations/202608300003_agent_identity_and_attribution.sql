-- Persistent OAuth subject resolution, agent-instance bindings, run lifecycle,
-- and content-minimized MCP tool-call attribution.
--
-- The transport verifies JWT signature/claims and calls the exact, session-aware
-- SECURITY DEFINER resolver below.  It does not receive SELECT access to identity
-- tables.  After resolution, ordinary application transactions set app.company_id,
-- as in the Phase 1 persistence migration.  Request payload fields never select
-- identity, role, cell, run, client, or protected resource.

CREATE TABLE IF NOT EXISTS public.anc_oauth_clients (
    client_id text PRIMARY KEY,
    company_id text NOT NULL,
    resource_url text NOT NULL CHECK (resource_url ~ '^https://[^?#]+/mcp$'),
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (company_id, client_id),
    CHECK (octet_length(client_id) BETWEEN 1 AND 256 AND client_id = btrim(client_id)),
    CHECK (char_length(company_id) BETWEEN 1 AND 256),
    CHECK (char_length(display_name) BETWEEN 1 AND 256),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS anc_oauth_clients_company_status_idx
    ON public.anc_oauth_clients (company_id, status, client_id);

CREATE TABLE IF NOT EXISTS public.anc_auth_subjects (
    company_id text NOT NULL,
    auth_subject_id text NOT NULL,
    issuer text NOT NULL,
    external_subject text NOT NULL,
    oauth_client_id text NOT NULL,
    principal_id text NOT NULL,
    subject_kind text NOT NULL CHECK (subject_kind IN ('agent', 'human', 'service')),
    status text NOT NULL CHECK (status IN ('active', 'disabled', 'revoked')),
    token_valid_after timestamptz,
    disabled_at timestamptz,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, auth_subject_id),
    UNIQUE (company_id, principal_id),
    UNIQUE (company_id, auth_subject_id, principal_id),
    CONSTRAINT anc_auth_subjects_external_identity_key
        UNIQUE (issuer, external_subject, oauth_client_id),
    FOREIGN KEY (company_id, oauth_client_id)
        REFERENCES public.anc_oauth_clients (company_id, client_id)
        ON DELETE RESTRICT,
    CHECK (char_length(company_id) BETWEEN 1 AND 256),
    CHECK (char_length(auth_subject_id) BETWEEN 1 AND 256),
    CHECK (char_length(principal_id) BETWEEN 1 AND 256),
    CHECK (octet_length(issuer) BETWEEN 1 AND 512 AND issuer = btrim(issuer)),
    CHECK (
        octet_length(external_subject) BETWEEN 1 AND 256
        AND external_subject = btrim(external_subject)
    ),
    CHECK (
        octet_length(oauth_client_id) BETWEEN 1 AND 256
        AND oauth_client_id = btrim(oauth_client_id)
    ),
    CHECK (updated_at >= created_at),
    CHECK (token_valid_after IS NULL OR token_valid_after >= created_at),
    CHECK (
        (status = 'active' AND disabled_at IS NULL)
        OR (status IN ('disabled', 'revoked') AND disabled_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS anc_auth_subjects_company_status_idx
    ON public.anc_auth_subjects (company_id, status, principal_id);

CREATE TABLE IF NOT EXISTS public.anc_agent_instances (
    company_id text NOT NULL,
    agent_instance_id text NOT NULL,
    principal_id text NOT NULL,
    definition_id text NOT NULL,
    definition_version integer NOT NULL CHECK (definition_version > 0),
    role text NOT NULL CHECK (
        role IN ('cell_owner', 'specialist', 'evaluator', 'allocator', 'immune', 'observer')
    ),
    cell_id text,
    status text NOT NULL CHECK (status IN ('active', 'suspended', 'retired')),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, agent_instance_id),
    UNIQUE (company_id, principal_id),
    UNIQUE (company_id, agent_instance_id, principal_id),
    FOREIGN KEY (company_id, principal_id)
        REFERENCES public.anc_auth_subjects (company_id, principal_id)
        ON DELETE RESTRICT,
    CHECK (char_length(agent_instance_id) BETWEEN 1 AND 256),
    CHECK (char_length(principal_id) BETWEEN 1 AND 256),
    CHECK (char_length(definition_id) BETWEEN 1 AND 256),
    CHECK (cell_id IS NULL OR char_length(cell_id) BETWEEN 1 AND 256),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS anc_agent_instances_company_status_idx
    ON public.anc_agent_instances (company_id, status, role, cell_id);

CREATE UNIQUE INDEX IF NOT EXISTS anc_agent_instances_active_cell_owner_idx
    ON public.anc_agent_instances (company_id, cell_id)
    WHERE role = 'cell_owner' AND status = 'active' AND cell_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.anc_agent_runs (
    company_id text NOT NULL,
    agent_run_id text NOT NULL,
    agent_instance_id text NOT NULL,
    principal_id text NOT NULL,
    definition_id text NOT NULL,
    definition_version integer NOT NULL CHECK (definition_version > 0),
    role text NOT NULL CHECK (
        role IN ('cell_owner', 'specialist', 'evaluator', 'allocator', 'immune', 'observer')
    ),
    cell_id text,
    status text NOT NULL CHECK (
        status IN ('running', 'succeeded', 'failed', 'cancelled', 'revoked', 'expired')
    ),
    termination_code text,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    started_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    ended_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, agent_run_id),
    UNIQUE (company_id, agent_run_id, agent_instance_id, principal_id),
    FOREIGN KEY (company_id, agent_instance_id, principal_id)
        REFERENCES public.anc_agent_instances
            (company_id, agent_instance_id, principal_id)
        ON DELETE RESTRICT,
    CHECK (char_length(agent_run_id) BETWEEN 1 AND 256),
    CHECK (char_length(agent_instance_id) BETWEEN 1 AND 256),
    CHECK (char_length(principal_id) BETWEEN 1 AND 256),
    CHECK (char_length(definition_id) BETWEEN 1 AND 256),
    CHECK (cell_id IS NULL OR char_length(cell_id) BETWEEN 1 AND 256),
    CHECK (termination_code IS NULL OR char_length(termination_code) BETWEEN 1 AND 128),
    CHECK (expires_at > started_at),
    CHECK (updated_at >= created_at),
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK (
        (status = 'running' AND ended_at IS NULL AND termination_code IS NULL)
        OR (status IN ('succeeded', 'failed', 'cancelled', 'revoked', 'expired')
            AND ended_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS anc_agent_runs_one_running_per_instance_idx
    ON public.anc_agent_runs (company_id, agent_instance_id)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS anc_agent_runs_principal_status_idx
    ON public.anc_agent_runs
        (company_id, principal_id, status, expires_at DESC);

CREATE TABLE IF NOT EXISTS public.anc_mcp_tool_calls (
    position bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id text NOT NULL,
    call_id text NOT NULL,
    phase text NOT NULL CHECK (phase IN ('received', 'succeeded', 'denied', 'failed')),
    request_id text NOT NULL,
    correlation_id text,
    principal_id text NOT NULL,
    auth_subject_id text,
    authentication_method text NOT NULL,
    authentication_id text NOT NULL,
    agent_instance_id text,
    agent_run_id text,
    definition_id text,
    definition_version integer,
    role text CHECK (
        role IN (
            'founder', 'cell_owner', 'specialist', 'evaluator',
            'allocator', 'immune', 'observer'
        )
    ),
    cell_id text,
    tool_name text NOT NULL,
    transport text NOT NULL,
    protocol_version text NOT NULL,
    arguments_sha256 character(64) NOT NULL,
    idempotency_key_sha256 character(64),
    authorization_outcome text NOT NULL CHECK (
        authorization_outcome IN ('pending', 'allowed', 'denied')
    ),
    authorization_reason_code text,
    execution_outcome text NOT NULL CHECK (
        execution_outcome IN ('pending', 'not_executed', 'succeeded', 'failed')
    ),
    error_code text,
    response_sha256 character(64),
    resource_type text,
    resource_id text,
    resource_revision bigint CHECK (resource_revision >= 0),
    received_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    duration_ms bigint CHECK (duration_ms >= 0),
    UNIQUE (company_id, call_id, phase),
    FOREIGN KEY (company_id, auth_subject_id, principal_id)
        REFERENCES public.anc_auth_subjects
            (company_id, auth_subject_id, principal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (company_id, agent_instance_id, principal_id)
        REFERENCES public.anc_agent_instances
            (company_id, agent_instance_id, principal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (company_id, agent_run_id, agent_instance_id, principal_id)
        REFERENCES public.anc_agent_runs
            (company_id, agent_run_id, agent_instance_id, principal_id)
        ON DELETE RESTRICT,
    CHECK (char_length(call_id) BETWEEN 1 AND 256),
    CHECK (char_length(request_id) BETWEEN 1 AND 256),
    CHECK (correlation_id IS NULL OR char_length(correlation_id) BETWEEN 1 AND 256),
    CHECK (char_length(authentication_method) BETWEEN 1 AND 128),
    CHECK (char_length(authentication_id) BETWEEN 1 AND 512),
    CHECK (char_length(tool_name) BETWEEN 1 AND 256),
    CHECK (char_length(transport) BETWEEN 1 AND 64),
    CHECK (char_length(protocol_version) BETWEEN 1 AND 64),
    CHECK (arguments_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (
        idempotency_key_sha256 IS NULL
        OR idempotency_key_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CHECK (response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (
        (agent_instance_id IS NULL
            AND agent_run_id IS NULL
            AND definition_id IS NULL
            AND definition_version IS NULL)
        OR (agent_instance_id IS NOT NULL
            AND agent_run_id IS NOT NULL
            AND definition_id IS NOT NULL
            AND definition_version > 0)
    ),
    CHECK (
        (phase = 'received'
            AND authorization_outcome = 'pending'
            AND authorization_reason_code IS NULL
            AND execution_outcome = 'pending'
            AND error_code IS NULL
            AND response_sha256 IS NULL
            AND resource_type IS NULL
            AND resource_id IS NULL
            AND resource_revision IS NULL
            AND duration_ms IS NULL
            AND recorded_at = received_at)
        OR (phase = 'denied'
            AND authorization_outcome = 'denied'
            AND authorization_reason_code IS NOT NULL
            AND execution_outcome = 'not_executed'
            AND duration_ms IS NOT NULL)
        OR (phase = 'succeeded'
            AND authorization_outcome = 'allowed'
            AND authorization_reason_code IS NOT NULL
            AND execution_outcome = 'succeeded'
            AND error_code IS NULL
            AND duration_ms IS NOT NULL)
        OR (phase = 'failed'
            AND authorization_outcome = 'allowed'
            AND authorization_reason_code IS NOT NULL
            AND execution_outcome = 'failed'
            AND error_code IS NOT NULL
            AND duration_ms IS NOT NULL)
    ),
    CHECK (
        authorization_reason_code IS NULL
        OR char_length(authorization_reason_code) BETWEEN 1 AND 128
    ),
    CHECK (error_code IS NULL OR char_length(error_code) BETWEEN 1 AND 128),
    CHECK (
        (resource_type IS NULL AND resource_id IS NULL AND resource_revision IS NULL)
        OR (resource_type IS NOT NULL AND resource_id IS NOT NULL)
    ),
    CHECK (recorded_at >= received_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS anc_mcp_tool_calls_one_terminal_idx
    ON public.anc_mcp_tool_calls (company_id, call_id)
    WHERE phase IN ('succeeded', 'denied', 'failed');

CREATE INDEX IF NOT EXISTS anc_mcp_tool_calls_company_position_idx
    ON public.anc_mcp_tool_calls (company_id, position);

CREATE INDEX IF NOT EXISTS anc_mcp_tool_calls_principal_time_idx
    ON public.anc_mcp_tool_calls
        (company_id, principal_id, recorded_at DESC);

CREATE INDEX IF NOT EXISTS anc_mcp_tool_calls_instance_time_idx
    ON public.anc_mcp_tool_calls
        (company_id, agent_instance_id, recorded_at DESC)
    WHERE agent_instance_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS anc_mcp_tool_calls_run_time_idx
    ON public.anc_mcp_tool_calls
        (company_id, agent_run_id, recorded_at DESC)
    WHERE agent_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS anc_mcp_tool_calls_request_idx
    ON public.anc_mcp_tool_calls (company_id, request_id);

CREATE OR REPLACE FUNCTION public.anc_protect_oauth_client()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'OAuth client history is retained; DELETE is forbidden'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.client_id, OLD.company_id, OLD.resource_url, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.client_id, NEW.company_id, NEW.resource_url, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'OAuth client identity and protected resource are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.revision <> OLD.revision + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'OAuth client updates require the next revision and monotonic time'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS anc_oauth_clients_identity_guard ON public.anc_oauth_clients;
CREATE TRIGGER anc_oauth_clients_identity_guard
BEFORE UPDATE OR DELETE ON public.anc_oauth_clients
FOR EACH ROW EXECUTE FUNCTION public.anc_protect_oauth_client();

CREATE OR REPLACE FUNCTION public.anc_protect_auth_subject()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'auth subject identity is retained; DELETE is forbidden'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.company_id, OLD.auth_subject_id, OLD.issuer,
        OLD.external_subject, OLD.oauth_client_id, OLD.principal_id,
        OLD.subject_kind, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.company_id, NEW.auth_subject_id, NEW.issuer,
        NEW.external_subject, NEW.oauth_client_id, NEW.principal_id,
        NEW.subject_kind, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'auth subject identity fields are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'revoked' AND NEW.status <> 'revoked' THEN
        RAISE EXCEPTION 'a revoked auth subject cannot be reactivated'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'disabled' AND NEW.status = 'active' AND (
        NEW.token_valid_after IS NULL OR NEW.token_valid_after < OLD.disabled_at
    ) THEN
        RAISE EXCEPTION 'reactivation must invalidate sessions from before disablement'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.token_valid_after IS NOT NULL AND (
        NEW.token_valid_after IS NULL OR NEW.token_valid_after < OLD.token_valid_after
    ) THEN
        RAISE EXCEPTION 'token_valid_after cannot move backward'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.revision <> OLD.revision + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'auth subject updates require the next revision and monotonic time'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS anc_auth_subjects_identity_guard ON public.anc_auth_subjects;
CREATE TRIGGER anc_auth_subjects_identity_guard
BEFORE UPDATE OR DELETE ON public.anc_auth_subjects
FOR EACH ROW EXECUTE FUNCTION public.anc_protect_auth_subject();

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
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS anc_agent_instances_subject_guard ON public.anc_agent_instances;
CREATE TRIGGER anc_agent_instances_subject_guard
BEFORE INSERT ON public.anc_agent_instances
FOR EACH ROW EXECUTE FUNCTION public.anc_validate_agent_instance_subject();

CREATE OR REPLACE FUNCTION public.anc_protect_agent_instance()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent instance identity is retained; DELETE is forbidden'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.company_id, OLD.agent_instance_id, OLD.principal_id,
        OLD.role, OLD.cell_id, OLD.created_at
    ) IS DISTINCT FROM ROW(
        NEW.company_id, NEW.agent_instance_id, NEW.principal_id,
        NEW.role, NEW.cell_id, NEW.created_at
    ) THEN
        RAISE EXCEPTION 'agent principal, role, and cell binding are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'retired' AND NEW.status <> 'retired' THEN
        RAISE EXCEPTION 'a retired agent instance cannot be reactivated'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'suspended' AND NEW.status = 'active' AND EXISTS (
        SELECT 1
        FROM public.anc_agent_runs AS run
        WHERE run.company_id = OLD.company_id
          AND run.agent_instance_id = OLD.agent_instance_id
          AND run.status = 'running'
    ) THEN
        RAISE EXCEPTION 'suspended agent runs must terminate before reactivation'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(OLD.definition_id, OLD.definition_version) IS DISTINCT FROM
       ROW(NEW.definition_id, NEW.definition_version) AND EXISTS (
            SELECT 1
            FROM public.anc_agent_runs AS run
            WHERE run.company_id = OLD.company_id
              AND run.agent_instance_id = OLD.agent_instance_id
              AND run.status = 'running'
       ) THEN
        RAISE EXCEPTION 'cannot change an agent definition while a run is active'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.revision <> OLD.revision + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'agent instance updates require the next revision and monotonic time'
            USING ERRCODE = '40001';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS anc_agent_instances_binding_guard ON public.anc_agent_instances;
CREATE TRIGGER anc_agent_instances_binding_guard
BEFORE UPDATE OR DELETE ON public.anc_agent_instances
FOR EACH ROW EXECUTE FUNCTION public.anc_protect_agent_instance();

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

DROP TRIGGER IF EXISTS anc_agent_runs_lifecycle_guard ON public.anc_agent_runs;
CREATE TRIGGER anc_agent_runs_lifecycle_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.anc_agent_runs
FOR EACH ROW EXECUTE FUNCTION public.anc_protect_agent_run();

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
            NEW.definition_version, NEW.tool_name,
            NEW.transport, NEW.protocol_version, NEW.arguments_sha256,
            NEW.idempotency_key_sha256, NEW.received_at
        ) IS DISTINCT FROM ROW(
            received_record.request_id, received_record.correlation_id,
            received_record.principal_id, received_record.auth_subject_id,
            received_record.authentication_method, received_record.authentication_id,
            received_record.agent_instance_id, received_record.agent_run_id,
            received_record.definition_id, received_record.definition_version,
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

DROP TRIGGER IF EXISTS anc_mcp_tool_calls_attribution_guard ON public.anc_mcp_tool_calls;
CREATE TRIGGER anc_mcp_tool_calls_attribution_guard
BEFORE INSERT ON public.anc_mcp_tool_calls
FOR EACH ROW EXECUTE FUNCTION public.anc_validate_mcp_tool_call_attribution();

DROP TRIGGER IF EXISTS anc_mcp_tool_calls_append_only ON public.anc_mcp_tool_calls;
CREATE TRIGGER anc_mcp_tool_calls_append_only
BEFORE UPDATE OR DELETE ON public.anc_mcp_tool_calls
FOR EACH ROW EXECUTE FUNCTION public.anc_reject_append_only_mutation();

ALTER TABLE public.anc_oauth_clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_oauth_clients FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_auth_subjects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_auth_subjects FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_agent_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_agent_instances FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_agent_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_mcp_tool_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_mcp_tool_calls FORCE ROW LEVEL SECURITY;

-- Provisioning and company administration remain company-scoped.  The runtime
-- resolves identity only through anc_resolve_agent_identity below.
DROP POLICY IF EXISTS anc_oauth_clients_select_company ON public.anc_oauth_clients;
CREATE POLICY anc_oauth_clients_select_company ON public.anc_oauth_clients
FOR SELECT
USING (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_oauth_clients_insert_company ON public.anc_oauth_clients;
CREATE POLICY anc_oauth_clients_insert_company ON public.anc_oauth_clients
FOR INSERT
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_oauth_clients_update_company ON public.anc_oauth_clients;
CREATE POLICY anc_oauth_clients_update_company ON public.anc_oauth_clients
FOR UPDATE
USING (company_id = NULLIF(current_setting('app.company_id', true), ''))
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_auth_subjects_select_company ON public.anc_auth_subjects;
CREATE POLICY anc_auth_subjects_select_company ON public.anc_auth_subjects
FOR SELECT
USING (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_auth_subjects_insert_company ON public.anc_auth_subjects;
CREATE POLICY anc_auth_subjects_insert_company ON public.anc_auth_subjects
FOR INSERT
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_auth_subjects_update_company ON public.anc_auth_subjects;
CREATE POLICY anc_auth_subjects_update_company ON public.anc_auth_subjects
FOR UPDATE
USING (company_id = NULLIF(current_setting('app.company_id', true), ''))
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

DO $policies$
DECLARE
    table_name text;
    policy_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['anc_agent_instances', 'anc_agent_runs']
    LOOP
        policy_name := table_name || '_company';
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', policy_name, table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL '
            'USING (company_id = NULLIF(current_setting(''app.company_id'', true), '''')) '
            'WITH CHECK (company_id = NULLIF(current_setting(''app.company_id'', true), ''''))',
            policy_name,
            table_name
        );
    END LOOP;
END;
$policies$;

DROP POLICY IF EXISTS anc_mcp_tool_calls_select_company ON public.anc_mcp_tool_calls;
CREATE POLICY anc_mcp_tool_calls_select_company ON public.anc_mcp_tool_calls
FOR SELECT
USING (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_mcp_tool_calls_insert_company ON public.anc_mcp_tool_calls;
CREATE POLICY anc_mcp_tool_calls_insert_company ON public.anc_mcp_tool_calls
FOR INSERT
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

-- Report only whether the current Supabase OAuth catalog has the exact relation,
-- column, and type contract used by the resolver.  SECURITY DEFINER lets the
-- runtime check compatibility without receiving direct auth-schema privileges.
CREATE OR REPLACE FUNCTION public.anc_auth_catalog_compatible()
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
        to_regclass('auth.oauth_clients') IS NOT NULL
        AND to_regclass('auth.sessions') IS NOT NULL
        AND to_regclass('auth.refresh_tokens') IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM (
                VALUES
                    (to_regclass('auth.oauth_clients'), 'id', 'uuid'::regtype),
                    (
                        to_regclass('auth.oauth_clients'),
                        'deleted_at',
                        'timestamptz'::regtype
                    ),
                    (to_regclass('auth.sessions'), 'id', 'uuid'::regtype),
                    (to_regclass('auth.sessions'), 'user_id', 'uuid'::regtype),
                    (
                        to_regclass('auth.sessions'),
                        'oauth_client_id',
                        'uuid'::regtype
                    ),
                    (
                        to_regclass('auth.sessions'),
                        'not_after',
                        'timestamptz'::regtype
                    ),
                    (
                        to_regclass('auth.sessions'),
                        'created_at',
                        'timestamptz'::regtype
                    ),
                    (
                        to_regclass('auth.refresh_tokens'),
                        'session_id',
                        'uuid'::regtype
                    ),
                    (
                        to_regclass('auth.refresh_tokens'),
                        'revoked',
                        'boolean'::regtype
                    )
            ) AS required_column(relation_id, column_name, type_id)
            WHERE NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_attribute AS attribute
                WHERE attribute.attrelid = required_column.relation_id
                  AND attribute.attname = required_column.column_name
                  AND attribute.atttypid = required_column.type_id
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
            )
        );
$function$;

REVOKE ALL ON FUNCTION public.anc_auth_catalog_compatible() FROM PUBLIC;

-- The fixed dynamic query keeps the migration executable in vanilla PostgreSQL
-- CI and fails closed until the hosted Supabase OAuth catalog exists.  Catalog
-- names and values are never interpolated; all verified claims remain parameters.
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
    -- Supabase owns this catalog and may change it independently.  An unsupported
    -- shape fails closed before the fixed dynamic statement is parsed.
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
REVOKE ALL ON TABLE
    public.anc_oauth_clients,
    public.anc_auth_subjects,
    public.anc_agent_instances,
    public.anc_agent_runs
    FROM PUBLIC;

-- Set the password only through deployment secrets.  This login is intentionally
-- unable to read identity tables; it receives the exact resolver and append-only
-- tool-call surface.  Existing domain-table grants remain an explicit deployment
-- concern until the production role migration is introduced.
DO $runtime_role$
DECLARE
    runtime_is_safe boolean;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'ai_nature_company_runtime'
    ) THEN
        CREATE ROLE ai_nature_company_runtime
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
    SELECT NOT (rolsuper OR rolbypassrls)
    INTO runtime_is_safe
    FROM pg_roles
    WHERE rolname = 'ai_nature_company_runtime';
    IF runtime_is_safe IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'ai_nature_company_runtime must be NOSUPERUSER and NOBYPASSRLS';
    END IF;
END;
$runtime_role$;

GRANT USAGE ON SCHEMA public TO ai_nature_company_runtime;
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
GRANT SELECT, INSERT ON TABLE public.anc_mcp_tool_calls
    TO ai_nature_company_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.anc_mcp_tool_calls_position_seq
    TO ai_nature_company_runtime;

COMMENT ON TABLE public.anc_oauth_clients IS
    'Exact Supabase OAuth client-to-protected-MCP-resource allowlist.';
COMMENT ON TABLE public.anc_auth_subjects IS
    'Immutable issuer/subject/client identity mapped to one server-owned principal; stores no bearer credential.';
COMMENT ON COLUMN public.anc_auth_subjects.token_valid_after IS
    'Reject otherwise-valid JWTs whose verified iat is earlier than this instant.';
COMMENT ON TABLE public.anc_agent_instances IS
    'One role/cell principal binding to a reusable, versioned agent definition.';
COMMENT ON TABLE public.anc_agent_runs IS
    'Revocable execution session with an immutable identity and definition snapshot.';
COMMENT ON TABLE public.anc_mcp_tool_calls IS
    'Append-only content-minimized MCP attribution; hashes payloads and stores no raw prompt, argument, response, or token.';

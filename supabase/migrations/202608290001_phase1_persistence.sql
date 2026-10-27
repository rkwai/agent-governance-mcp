-- Phase 1 deterministic substrate persistence.
--
-- Every application transaction must set a local company scope with:
--   SELECT set_config('app.company_id', '<company-id>', true);
-- The RLS predicates intentionally evaluate to false when that setting is absent.
-- Run the application as a non-superuser without BYPASSRLS; PostgreSQL superusers
-- always bypass row-level security, including FORCE ROW LEVEL SECURITY.

CREATE TABLE IF NOT EXISTS public.anc_stream_heads (
    company_id text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    PRIMARY KEY (company_id, aggregate_type, aggregate_id)
);

CREATE TABLE IF NOT EXISTS public.anc_domain_events (
    position bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id text NOT NULL,
    event_id text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision > 0),
    actor_id text NOT NULL,
    cell_id text,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text,
    correlation_id text,
    causation_id text,
    UNIQUE (company_id, event_id),
    UNIQUE (company_id, aggregate_type, aggregate_id, aggregate_revision)
);

CREATE INDEX IF NOT EXISTS anc_domain_events_company_position_idx
    ON public.anc_domain_events (company_id, position);
CREATE INDEX IF NOT EXISTS anc_domain_events_stream_idx
    ON public.anc_domain_events
    (company_id, aggregate_type, aggregate_id, aggregate_revision);
CREATE INDEX IF NOT EXISTS anc_domain_events_correlation_idx
    ON public.anc_domain_events (company_id, correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.anc_audit_events (
    position bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id text NOT NULL,
    audit_id text NOT NULL,
    action text NOT NULL,
    outcome text NOT NULL,
    actor_id text NOT NULL,
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    occurred_at timestamptz NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    correlation_id text,
    UNIQUE (company_id, audit_id)
);

CREATE INDEX IF NOT EXISTS anc_audit_events_company_position_idx
    ON public.anc_audit_events (company_id, position);
CREATE INDEX IF NOT EXISTS anc_audit_events_resource_idx
    ON public.anc_audit_events (company_id, resource_type, resource_id, position);

CREATE TABLE IF NOT EXISTS public.anc_projections (
    company_id text NOT NULL,
    projection_type text NOT NULL,
    projection_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, projection_type, projection_id)
);

CREATE INDEX IF NOT EXISTS anc_projections_company_type_id_idx
    ON public.anc_projections (company_id, projection_type, projection_id);

CREATE TABLE IF NOT EXISTS public.anc_idempotency_records (
    company_id text NOT NULL,
    namespace text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    response jsonb,
    error jsonb,
    revision bigint NOT NULL CHECK (revision > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    expires_at timestamptz,
    PRIMARY KEY (company_id, namespace, idempotency_key),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    CHECK (
        (status = 'in_progress' AND response IS NULL AND error IS NULL)
        OR (status = 'completed' AND response IS NOT NULL AND error IS NULL)
        OR (status = 'failed' AND response IS NULL AND error IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS anc_idempotency_expiry_idx
    ON public.anc_idempotency_records (company_id, expires_at)
    WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.anc_budgets (
    company_id text NOT NULL,
    budget_id text NOT NULL,
    scope text NOT NULL,
    limit_amount numeric(30, 9) NOT NULL CHECK (limit_amount >= 0),
    spent_amount numeric(30, 9) NOT NULL DEFAULT 0 CHECK (spent_amount >= 0),
    reserved_amount numeric(30, 9) NOT NULL DEFAULT 0 CHECK (reserved_amount >= 0),
    currency text NOT NULL CHECK (char_length(currency) = 3),
    cell_id text,
    agent_id text,
    experiment_id text,
    status text NOT NULL CHECK (status IN ('active', 'exhausted', 'closed')),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, budget_id),
    CHECK (spent_amount + reserved_amount <= limit_amount),
    CHECK (ends_at IS NULL OR ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS anc_budgets_scope_idx
    ON public.anc_budgets (company_id, scope, status);

CREATE TABLE IF NOT EXISTS public.anc_budget_reservations (
    company_id text NOT NULL,
    reservation_id text NOT NULL,
    budget_id text NOT NULL,
    amount numeric(30, 9) NOT NULL CHECK (amount > 0),
    actual_amount numeric(30, 9),
    currency text NOT NULL CHECK (char_length(currency) = 3),
    purpose text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'committed', 'released')),
    idempotency_key text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    expires_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, reservation_id),
    FOREIGN KEY (company_id, budget_id)
        REFERENCES public.anc_budgets (company_id, budget_id)
        ON DELETE RESTRICT,
    CHECK (actual_amount IS NULL OR (actual_amount >= 0 AND actual_amount <= amount)),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    CHECK (
        (status = 'active' AND actual_amount IS NULL)
        OR status = 'released'
        OR (status = 'committed' AND actual_amount IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS anc_budget_reservations_budget_idx
    ON public.anc_budget_reservations (company_id, budget_id, status);
CREATE INDEX IF NOT EXISTS anc_budget_reservations_expiry_idx
    ON public.anc_budget_reservations (company_id, expires_at)
    WHERE status = 'active' AND expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.anc_work_leases (
    company_id text NOT NULL,
    work_item_id text NOT NULL,
    lease_id text NOT NULL,
    agent_id text NOT NULL,
    agent_run_id text,
    cell_id text,
    token text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'released')),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    acquired_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    released_at timestamptz,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (company_id, work_item_id),
    UNIQUE (company_id, lease_id),
    CHECK (expires_at > acquired_at),
    CHECK (
        (status = 'active' AND released_at IS NULL)
        OR (status = 'released' AND released_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS anc_work_leases_agent_idx
    ON public.anc_work_leases (company_id, agent_id, agent_run_id, status, expires_at);

CREATE OR REPLACE FUNCTION public.anc_reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION '% is append-only; % is forbidden', TG_TABLE_NAME, TG_OP
        USING ERRCODE = '55000';
END;
$function$;

DROP TRIGGER IF EXISTS anc_domain_events_append_only ON public.anc_domain_events;
CREATE TRIGGER anc_domain_events_append_only
BEFORE UPDATE OR DELETE ON public.anc_domain_events
FOR EACH ROW EXECUTE FUNCTION public.anc_reject_append_only_mutation();

DROP TRIGGER IF EXISTS anc_audit_events_append_only ON public.anc_audit_events;
CREATE TRIGGER anc_audit_events_append_only
BEFORE UPDATE OR DELETE ON public.anc_audit_events
FOR EACH ROW EXECUTE FUNCTION public.anc_reject_append_only_mutation();

ALTER TABLE public.anc_stream_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_stream_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_domain_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_domain_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_projections FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_idempotency_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_idempotency_records FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_budgets FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_budget_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_budget_reservations FORCE ROW LEVEL SECURITY;
ALTER TABLE public.anc_work_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anc_work_leases FORCE ROW LEVEL SECURITY;

-- Append-only tables get no UPDATE or DELETE policy.  The trigger is a second
-- defense for table owners and privileged maintenance sessions.
DROP POLICY IF EXISTS anc_domain_events_select_company ON public.anc_domain_events;
CREATE POLICY anc_domain_events_select_company ON public.anc_domain_events
FOR SELECT
USING (company_id = NULLIF(current_setting('app.company_id', true), ''));
DROP POLICY IF EXISTS anc_domain_events_insert_company ON public.anc_domain_events;
CREATE POLICY anc_domain_events_insert_company ON public.anc_domain_events
FOR INSERT
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

DROP POLICY IF EXISTS anc_audit_events_select_company ON public.anc_audit_events;
CREATE POLICY anc_audit_events_select_company ON public.anc_audit_events
FOR SELECT
USING (company_id = NULLIF(current_setting('app.company_id', true), ''));
DROP POLICY IF EXISTS anc_audit_events_insert_company ON public.anc_audit_events;
CREATE POLICY anc_audit_events_insert_company ON public.anc_audit_events
FOR INSERT
WITH CHECK (company_id = NULLIF(current_setting('app.company_id', true), ''));

DO $policies$
DECLARE
    table_name text;
    policy_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'anc_stream_heads',
        'anc_projections',
        'anc_idempotency_records',
        'anc_budgets',
        'anc_budget_reservations',
        'anc_work_leases'
    ]
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

COMMENT ON TABLE public.anc_domain_events IS
    'Append-only company-scoped domain event log for deterministic replay.';
COMMENT ON TABLE public.anc_audit_events IS
    'Append-only record of attributable decisions and external-action outcomes.';
COMMENT ON COLUMN public.anc_work_leases.token IS
    'Opaque capability checked by constant-time comparison in the application.';

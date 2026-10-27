-- Add correlated constraint ledgers and atomic logical reservation groups.
--
-- A reservation group mirrors one economic hold across company, cell, and stage
-- ledgers.  Mirrored rows must never be summed as separate costs.

ALTER TABLE public.anc_budgets
    ADD COLUMN IF NOT EXISTS parent_budget_id text,
    ADD COLUMN IF NOT EXISTS stage text;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budgets_parent_fk'
          AND conrelid = 'public.anc_budgets'::regclass
    ) THEN
        ALTER TABLE public.anc_budgets
            ADD CONSTRAINT anc_budgets_parent_fk
            FOREIGN KEY (company_id, parent_budget_id)
            REFERENCES public.anc_budgets (company_id, budget_id)
            ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budgets_stage_scope_check'
          AND conrelid = 'public.anc_budgets'::regclass
    ) THEN
        ALTER TABLE public.anc_budgets
            ADD CONSTRAINT anc_budgets_stage_scope_check
            CHECK (
                (scope = 'stage' AND stage IN ('exploration', 'exploitation'))
                OR (scope <> 'stage' AND stage IS NULL)
            ) NOT VALID;
        ALTER TABLE public.anc_budgets
            VALIDATE CONSTRAINT anc_budgets_stage_scope_check;
    END IF;
END;
$constraints$;

CREATE INDEX IF NOT EXISTS anc_budgets_parent_idx
    ON public.anc_budgets (company_id, parent_budget_id)
    WHERE parent_budget_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS anc_budgets_active_stage_idx
    ON public.anc_budgets (company_id, stage)
    WHERE scope = 'stage' AND status = 'active';

ALTER TABLE public.anc_budget_reservations
    ADD COLUMN IF NOT EXISTS reservation_group_id text,
    ADD COLUMN IF NOT EXISTS ledger_role text;

UPDATE public.anc_budget_reservations
SET reservation_group_id = reservation_id
WHERE reservation_group_id IS NULL;

UPDATE public.anc_budget_reservations
SET ledger_role = 'budget'
WHERE ledger_role IS NULL;

ALTER TABLE public.anc_budget_reservations
    ALTER COLUMN reservation_group_id SET NOT NULL,
    ALTER COLUMN ledger_role SET NOT NULL;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budget_reservations_ledger_role_check'
          AND conrelid = 'public.anc_budget_reservations'::regclass
    ) THEN
        ALTER TABLE public.anc_budget_reservations
            ADD CONSTRAINT anc_budget_reservations_ledger_role_check
            CHECK (char_length(ledger_role) > 0);
    END IF;
END;
$constraints$;

CREATE UNIQUE INDEX IF NOT EXISTS anc_budget_reservations_group_role_idx
    ON public.anc_budget_reservations
        (company_id, reservation_group_id, ledger_role);

CREATE UNIQUE INDEX IF NOT EXISTS anc_budget_reservations_group_budget_idx
    ON public.anc_budget_reservations
        (company_id, reservation_group_id, budget_id);

CREATE INDEX IF NOT EXISTS anc_budget_reservations_group_idx
    ON public.anc_budget_reservations (company_id, reservation_group_id, status);

COMMENT ON COLUMN public.anc_budgets.parent_budget_id IS
    'Structural parent for currency/window validation; economic holds still charge every constraint ledger explicitly.';
COMMENT ON COLUMN public.anc_budgets.stage IS
    'Company-global exploration or exploitation constraint ledger.';
COMMENT ON COLUMN public.anc_budget_reservations.reservation_group_id IS
    'Logical economic hold mirrored across constraint ledgers; member amounts are not additive.';

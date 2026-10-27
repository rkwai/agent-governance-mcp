-- Add an explicit unit of account to constraint ledgers.
--
-- Existing USD rows stay unit=money with their ISO currency. Time, cycles, and
-- tries store whole canonical integers in the same numeric amount columns
-- (seconds or counts) and leave currency NULL. TRY remains Turkish lira;
-- attempts use unit=tries. Do not apply this to production from application code.

ALTER TABLE public.anc_budgets
    ADD COLUMN IF NOT EXISTS unit text NOT NULL DEFAULT 'money';

ALTER TABLE public.anc_budget_reservations
    ADD COLUMN IF NOT EXISTS unit text NOT NULL DEFAULT 'money';

DO $constraints$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'public.anc_budgets'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%char_length(currency) = 3%'
    LOOP
        EXECUTE format('ALTER TABLE public.anc_budgets DROP CONSTRAINT %I', rec.conname);
    END LOOP;
    FOR rec IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'public.anc_budget_reservations'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%char_length(currency) = 3%'
    LOOP
        EXECUTE format(
            'ALTER TABLE public.anc_budget_reservations DROP CONSTRAINT %I',
            rec.conname
        );
    END LOOP;
END;
$constraints$;

ALTER TABLE public.anc_budgets
    ALTER COLUMN currency DROP NOT NULL;

ALTER TABLE public.anc_budget_reservations
    ALTER COLUMN currency DROP NOT NULL;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budgets_unit_check'
          AND conrelid = 'public.anc_budgets'::regclass
    ) THEN
        ALTER TABLE public.anc_budgets
            ADD CONSTRAINT anc_budgets_unit_check
            CHECK (unit IN ('money', 'time', 'cycles', 'tries')) NOT VALID;
        ALTER TABLE public.anc_budgets
            VALIDATE CONSTRAINT anc_budgets_unit_check;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budgets_unit_currency_check'
          AND conrelid = 'public.anc_budgets'::regclass
    ) THEN
        ALTER TABLE public.anc_budgets
            ADD CONSTRAINT anc_budgets_unit_currency_check
            CHECK (
                (unit = 'money' AND currency IS NOT NULL AND char_length(currency) = 3)
                OR (unit <> 'money' AND currency IS NULL)
            ) NOT VALID;
        ALTER TABLE public.anc_budgets
            VALIDATE CONSTRAINT anc_budgets_unit_currency_check;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budget_reservations_unit_check'
          AND conrelid = 'public.anc_budget_reservations'::regclass
    ) THEN
        ALTER TABLE public.anc_budget_reservations
            ADD CONSTRAINT anc_budget_reservations_unit_check
            CHECK (unit IN ('money', 'time', 'cycles', 'tries')) NOT VALID;
        ALTER TABLE public.anc_budget_reservations
            VALIDATE CONSTRAINT anc_budget_reservations_unit_check;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budget_reservations_unit_currency_check'
          AND conrelid = 'public.anc_budget_reservations'::regclass
    ) THEN
        ALTER TABLE public.anc_budget_reservations
            ADD CONSTRAINT anc_budget_reservations_unit_currency_check
            CHECK (
                (unit = 'money' AND currency IS NOT NULL AND char_length(currency) = 3)
                OR (unit <> 'money' AND currency IS NULL)
            ) NOT VALID;
        ALTER TABLE public.anc_budget_reservations
            VALIDATE CONSTRAINT anc_budget_reservations_unit_currency_check;
    END IF;
END;
$constraints$;

COMMENT ON COLUMN public.anc_budgets.unit IS
    'Ledger unit of account: money, time (seconds), cycles, or tries. One row, one unit.';
COMMENT ON COLUMN public.anc_budgets.currency IS
    'ISO 4217 currency when unit is money; NULL for time, cycles, and tries.';
COMMENT ON COLUMN public.anc_budget_reservations.unit IS
    'Copied from the reserved budget. A tries reservation cannot consume a money ledger.';
COMMENT ON COLUMN public.anc_budget_reservations.currency IS
    'ISO 4217 currency when unit is money; NULL otherwise.';

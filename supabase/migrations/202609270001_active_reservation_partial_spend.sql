-- Allow incremental Act spend against an active reservation group.
--
-- apply_reservation_group_spend records actual_amount while status stays
-- active so later sends can debit the same hold. The original table check
-- required actual_amount IS NULL whenever status = 'active', which crashed
-- the first production experiment.record_send (CheckViolation
-- anc_budget_reservations_check2) and rolled the transaction back.

ALTER TABLE public.anc_budget_reservations
    DROP CONSTRAINT IF EXISTS anc_budget_reservations_check2;

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'anc_budget_reservations_active_partial_spend_check'
          AND conrelid = 'public.anc_budget_reservations'::regclass
    ) THEN
        ALTER TABLE public.anc_budget_reservations
            ADD CONSTRAINT anc_budget_reservations_active_partial_spend_check
            CHECK (
                status = 'active'
                OR status = 'released'
                OR (status = 'committed' AND actual_amount IS NOT NULL)
            );
    END IF;
END;
$constraints$;

COMMENT ON CONSTRAINT anc_budget_reservations_active_partial_spend_check
    ON public.anc_budget_reservations IS
    'Active holds may accumulate actual_amount from incremental try spend; committed rows still require actual_amount.';

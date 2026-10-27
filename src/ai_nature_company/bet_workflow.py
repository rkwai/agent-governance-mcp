"""Host-neutral discovery contract; instructions never grant operating authority."""

from __future__ import annotations

from .forces import LEDGER_HANDOFF

CELL_RECORD_KINDS = (
    "work_item",
    "offer",
    "experiment",
    "evidence",
    "learning",
    "experiment_send",
)
MAX_CELL_RECORDS = 50
MAX_CELL_RECORD_BYTES = 262_144


def describe_bet_workflow() -> dict[str, object]:
    """Return fresh, bounded operating guidance for an authenticated MCP host."""

    return {
        "version": 1,
        "identity_activation": {
            "first_call": "company.describe",
            "payload": {},
            "compare_to_operator_inventory": [
                "principal_id",
                "agent_instance_id",
                "definition_id",
                "definition_version",
                "role",
                "cell_id",
                "agent_run_id",
            ],
            "on_mismatch": "Stop; report non-secret mismatches; do not try other connectors.",
            "then": ["constitution.get", "protocol.describe"],
        },
        "context_read": {
            "tool": "company.describe",
            "payload": {
                "cell_records": {
                    "cell_id": "<authorized-cell-uuid>",
                    "kind": "<record-kind>",
                    "limit": 20,
                    "after_id": None,
                }
            },
            "kinds": list(CELL_RECORD_KINDS),
            "max_records": MAX_CELL_RECORDS,
            "max_record_bytes_per_page": MAX_CELL_RECORD_BYTES,
            "response": "cell_records only; records have record_id, revision, state",
            "revision": "Mutation precondition: offer.version - 1 for offer.mutate; "
            "otherwise state.revision when present, then stored projection revision. "
            "Offer input references use state.version, not this mutation precondition.",
            "pagination": "Repeat the same cell and kind with next_after_id until null.",
            "consistency": (
                "A page is observed state, not a multi-page snapshot. Refresh affected records "
                "before mutations; expected_revision still applies. Empty pages are not success."
            ),
            "experiment_evaluation": "An experiment record includes its linked evaluation, if any.",
            "secrets": "No lease tokens, runtime projections, or identity tables are returned.",
        },
        "operate": [
            "Read the cell's offers, experiments, work and evidence before proposing new work.",
            "Keep unrelated commercial bets in separate cells with separate offer lineage.",
            "Register hypothesis, metrics, thresholds, window, stop rules, budget unit and "
            "independent evaluator before experiment.start; do not retrofit criteria to results.",
            "Owner-signed experiment.amend may extend a running window, revise stop-rule, "
            "treatment, or hypothesis window text, change budget_amount_minor through "
            "the reservation ledger, or set an experiment max_external_messages override "
            "with founder_decision_ref. It is running-only and must not retrofit metrics, "
            "thresholds, budget unit, or evaluator. Defend judges the clock against "
            "window_ends_at, not hypothesis prose.",
            "Owner-signed cell.amend may revise the cell's objective text with "
            "founder_decision_ref. Own active non-terminal cell only. It must not "
            "change budget, stage, owner, authority_policy, or constitution. Prior "
            "objective stays in amendment_history.",
            "Owner-signed cell.transition to killed executes a recorded founder "
            "decision: founder_decision_ref is required, reason is optional, and the "
            "cell must have no open experiments and no open or claimed work. Without "
            "founder_decision_ref, kill stays founder-only. The ref is recorded on the "
            "kill decision (decision.explain) and audit.list.",
            "Owner-signed work.cancel closes never-claimed work on the owner's own "
            "cell when a non-empty reason is recorded. It refuses claimed or leased "
            "work and other cells. The reason stays in cancellation_history. "
            "Cancelled work is terminal and leaves stale_open_work_count.",
            "Owner-signed experiment.record_send records one founder-signed external send "
            "and debits exactly one try. Idempotent per send_id; refuses double counting. "
            "sent_at is the founder/Gmail-verified send time and is not matched to "
            "evidence timestamps. Read sends back via company.describe cell_records "
            "kind experiment_send (includes decision_id) or cell-scoped audit.list. "
            "Draft Act work is not a send. Sense does not debit.",
            "experiment.close may record optional reason and founder_decision_ref. "
            "They persist on the experiment and audit trail and appear on "
            "company.describe / audit.list. Existing callers without those fields "
            "keep working.",
            "evaluation.submit may record a count criterion as failed with empty "
            "evidence_ids when observed_value is 0 and the ledger has no qualifying "
            "fitness evidence. Passed still requires evidence. Empty-evidence "
            "failure is refused when qualifying fitness evidence exists.",
            "Claim only eligible work; inspect its outcome, references, approvals and budget. "
            "A listed work item is not a lease or a permission grant.",
            "Keep the lease alive; stop on expiry, revocation, containment or exhausted budget.",
            "Append actual evidence with source, time, lineage and honest provenance; record "
            "cost and negative outcomes. Do not convert historical reports into verified receipts.",
            "Complete, fail or release work with an attributable result; task completion is "
            "not experiment success.",
            "After a mutate that advances cell, run, evidence, lease or tries: stop or "
            "read back via company.describe / cell_records. The ledger write is the "
            "handoff. Do not DM a peer or use agent chat for routine Sense, Act or Bound "
            "handoffs. Channel membership is not a work queue. Force-peer silence is "
            "not silence toward founder, humans, or other external entities; "
            "acknowledge, do the work, and reply when they are waiting.",
        ],
        "handoff": dict(LEDGER_HANDOFF),
        "forces": {
            "kind": "physics",
            "names": [
                "bound",
                "sense",
                "act",
                "transform",
                "maintain",
                "defend",
                "select",
                "continue",
            ],
            "sense_does_not_debit_try_spend": True,
            "act_complete_fail_debits_try_spend": True,
            "select_reads": "fitness_evidence_count",
            "producer_cannot_clear_own_claims": True,
            "hosts_and_agents": "temporary_particles_inside_forces",
        },
        "work_cancel": {
            "tool": "work.cancel",
            "authority": "owner_signed",
            "requires": "reason",
            "optional": ["superseded_by"],
            "valid_state": "open",
            "own_cell_only": True,
            "never_claimed": True,
            "no_active_lease": True,
            "terminal_status": "cancelled",
            "may": ["close_never_claimed_open_work"],
            "must_not": [
                "claimed_or_leased_work",
                "cancel_other_cell_work",
                "debit_try_spend",
                "rewrite_history",
            ],
        },
        "cell_kill": {
            "tool": "cell.transition",
            "target_status": "killed",
            "authority": "founder_or_owner_signed",
            "owner_signed_requires": "founder_decision_ref",
            "optional": ["reason"],
            "valid_state": ["active", "paused"],
            "owner_signed_requires_quiescent": {
                "open_experiments": 0,
                "open_or_claimed_work": 0,
            },
            "read_back": ["cell.get", "decision.explain", "audit.list"],
        },
        "cell_amend": {
            "tool": "cell.amend",
            "authority": "owner_signed",
            "requires": "founder_decision_ref",
            "valid_state": "active",
            "own_cell_only": True,
            "may": ["revise_objective"],
            "must_not": [
                "change_budget",
                "change_stage_or_owner",
                "change_authority_or_constitution",
            ],
        },
        "experiment_amend": {
            "tool": "experiment.amend",
            "authority": "owner_signed",
            "requires": "founder_decision_ref",
            "valid_state": "running",
            "own_cell_only": True,
            "may": [
                "extend_window_end",
                "revise_stop_rules",
                "revise_treatment",
                "align_hypothesis_window_text",
                "set_budget_amount_minor",
                "set_max_external_messages",
            ],
            "must_not": [
                "retrofit_evaluation_criteria",
                "change_budget_unit",
                "change_evaluator_or_cell",
                "amend_constitution_or_risk_policy",
            ],
            "hypothesis_dates_are_not": "the_clock",
            "message_cap": {
                "field": "max_external_messages",
                "ceiling": "amended_or_registered_try_budget",
                "floor": "recorded_send_count",
                "record_send_uses": "override_when_present_else_constitution",
            },
            "budget_amount": {
                "field": "budget_amount_minor",
                "path": "reservation_ledger",
                "floor": "already_spent",
                "ceiling": "constitution_experiment_limit",
            },
        },
        "experiment_record_send": {
            "tool": "experiment.record_send",
            "force": "act",
            "authority": "owner_signed",
            "requires": ["founder_signoff_ref", "evidence_id", "send_id"],
            "spend": "exactly_one_try",
            "idempotent_per": "send_id",
            "refuses": ["double_count", "external_message_cap"],
            "enforces": "experiment_or_constitution_message_cap",
            "remaining_field": "external_messages_remaining",
            "valid_state": "running",
            "does_not": [
                "debit_sense",
                "treat_draft_work_as_a_send",
                "silently_block",
                "silently_fail_to_block",
                "cross_validate_sent_at_against_evidence_timestamps",
            ],
            "read_back": [
                "company.describe cell_records kind experiment_send",
                "cell-scoped audit.list",
                "decision.explain",
            ],
        },
        "experiment_close": {
            "tool": "experiment.close",
            "optional": ["reason", "founder_decision_ref"],
            "valid_state": "running",
            "persists_on": ["experiment", "audit.list", "company.describe"],
            "backward_compatible_without_optional_fields": True,
        },
        "evaluation": {
            "force": "defend",
            "independent_principal_required": True,
            "tools": ["experiment.close", "evaluation.submit", "gate.evaluate"],
            "tick": "Maintain/Continue closeout creates evaluate_experiment work; "
            "hosts discover it here.",
            "review": "Use frozen criteria and cited evidence, including negative evidence; "
            "missing proof is not a pass.",
            "judges_window_against": "window_ends_at",
            "does_not_parse": "hypothesis_dates",
            "zero_evidence_failure": {
                "metric_unit": "count",
                "outcome": "failed",
                "observed_value": 0,
                "evidence_ids": [],
                "requires": "no_qualifying_fitness_evidence",
                "passed_still_requires_evidence": True,
            },
        },
        "selection": {
            "force": "select",
            "tools": ["allocation.propose", "allocation.commit"],
            "controller": "allocator when provisioned; bounded single-cell owner path otherwise",
            "rules": "Eligibility before fitness; Select reads fitness not activity; "
            "exploration and exploitation stay separate; irreversible kill and constitution "
            "changes require founder authority. Owner-led contain does not impersonate Select.",
        },
        "retry": {
            "uncertain_result": "Retry the exact mutation with the same idempotency_key.",
            "revision_conflict": "Read current state, reconcile, then submit a new intent/key.",
            "forbidden": "Do not switch identities, forge approval, or bypass MCP with SQL.",
        },
        "external_action_authority": "not_granted_by_discovery_or_work_lease",
        "external_actions": (
            "The current substrate records approved actions and evidence; discovery does not "
            "send messages or authorize outreach, CRM writes, spend, hosting or client data use. "
            "Honor applicable founder approvals in the host and adapter as well as "
            "the constitution."
        ),
        "blocked_paths": ["cell.scale", "cell.unpause", "learning.promote"],
        "learning": "learning.propose persists a candidate; it does not promote policy or memory.",
    }

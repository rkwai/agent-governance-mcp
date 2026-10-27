# Operate and resume a bet through MCP

This is the operating entry point for any AI host. Grok is the current closed-beta
host; the company, cell, experiment and evidence contracts do not depend on Grok.
The same guide is returned in structured form by `protocol.describe` under
`bet_workflow`. Deployment and connector provisioning are kept out of this public copy.

## Start with verified identity and current state

1. Select the connector assigned in the operator inventory. Call `company.describe`
   with an empty payload. Compare the returned principal, instance, definition and
   version, role, cell and run to the expected binding. Stop and report non-secret
   mismatches; do not try another identity to get a call through.
2. Read `constitution.get` and `protocol.describe`. Check the current budget unit,
   price floor, approval requirements, operating state and effective capabilities.
   A discovered tool is not an authority grant. Do not infer live deployment or
   commercial success from repository examples or an accessible connector.
3. Read the relevant cell's records. Resume existing work before proposing a new
   experiment or duplicating a sent packet. Keep separate commercial bets in
   separate cells and immutable offer lineages. Actual cell IDs come from MCP and
   the operator inventory, never from an invented mapping in a planning document.

## Retrieve enough context to act or evaluate independently

Existing v1 agent definitions already declare `company.describe`. Its optional
`cell_records` query makes work, frozen experiment criteria and evidence available
without replacing those definitions or reprovisioning identities.

Call `company.describe` with this tool payload (UUID shown is a placeholder):

```json
{
  "cell_records": {
    "cell_id": "00000000-0000-4000-8000-000000000001",
    "kind": "work_item",
    "limit": 20,
    "after_id": null
  }
}
```

The MCP tool argument is named `payload`: when calling through the SDK, wrap the
object above as `{"payload": {"cell_records": { ... }}}`.

The response contains only `cell_records`: `cell_id`, `kind`, `observed_at`,
`records`, and `next_after_id`. Each record has `record_id`, `revision`, and full
`state`. An experiment record also includes its linked independent `evaluation`
when one exists. An empty-payload `company.describe` keeps its identity and
portfolio response unchanged.

| `kind` | Read before |
|---|---|
| `offer` | Choosing the immutable offer version or proposing a mutation. |
| `experiment` | Starting, working, closing or evaluating a frozen experiment. |
| `work_item` | Claiming eligible work, resuming a run, or finding tick-created evaluator closeout work. |
| `evidence` | Evaluating a criterion or proposing a lesson; review provenance, corrections, negative evidence and source timing. |
| `learning` | Reusing or proposing learning; `candidate` does not mean promoted. |
| `experiment_send` | Inspecting owner-recorded founder-signed external sends and try spend. Includes `decision_id`. `sent_at` is Gmail/founder-verified and is not matched to evidence timestamps. |

Pagination filters by the authenticated company and authorized cell before applying
the page limit. It never uses another cell's IDs as pagination cursors. Repeat the
same query using `next_after_id` until it is null. The limit is 1–50 records, default
20, with a 256 KiB record-data ceiling per page. Whole records are carried forward
to the next page rather than truncating frozen criteria or evidence. An oversized
single record returns an explicit error. Runtime projections, lease tokens and
identity tables are not available through this query.

Each page reflects its own `observed_at`; multiple pages are not one frozen
snapshot. Refresh affected state before a mutation. Use the returned record's
`revision` for that aggregate's `expected_revision`: an offer uses `state.version - 1`
because `offer.mutate` consumes that lineage precondition (v1 → 0, v3 → 2), not the
immutable offer projection's storage revision. Other kinds use `state.revision`
when present, otherwise their projection revision. Offer input references still
use the actual `state.version`; preserve each reference's own revision convention.
A listed `claimed` work item is not your lease; only the claiming
principal/run has its token. An empty result means no matching records were read,
not that a customer demand test passed or no work exists in another authorized cell.

## Run one bounded learning loop

| Stage | Operator action | Durable result |
|---|---|---|
| Frame | Owner selects a customer job and offer version; state the uncertainty that the experiment can resolve. | Immutable offer and preregistered experiment. |
| Bound | Before `experiment.start`, register hypothesis, population, metrics, success/failure thresholds, stop rule, window, allowed actions, budget and independent evaluator. A running experiment's window, stop-rule text, treatment, hypothesis window phrasing, try-budget amount, or experiment message cap may be owner-amended via `experiment.amend` with `founder_decision_ref`; metrics, thresholds, budget unit, and evaluator stay frozen. Defend judges the clock against `window_ends_at`. After a founder-signed external send, record it with `experiment.record_send` so the try ledger moves. Read `external_messages_remaining` before recording; the experiment `max_external_messages` override, or else the stored constitution cap, refuses the next send explicitly when remaining is 0. The cell-owner may revise its own active cell's objective text via `cell.amend` with `founder_decision_ref`; budget, stage, owner, authority, and constitution stay frozen. To carry out a founder kill decision, the cell-owner may `cell.transition` its cell to `killed` with a required `founder_decision_ref` (optional `reason`) once no experiments are open and no work is open or claimed; without the ref, kill stays founder-only. | Frozen protocol and attributable budget reservation; amended window end is what the tick reads; each recorded send spends one try and counts against the message cap; amended cell objective stays inspectable with prior text in amendment history. |
| Work | Create and claim a narrow work item. Check acceptance, input references, approvals, budget and active lease before acting. The bound cell owner may `work.cancel` never-claimed leftover items on that cell with a recorded reason; claimed or leased work stays with the lease holder. | Work contract and one expiring lease; cancelled leftovers leave `stale_open_work_count`. |
| Observe | Append actual customer behavior, source references, timing, costs, exceptions and corrections. Label manual attestations honestly. | Append-only evidence and economics, including negative results. |
| Close | Stop at the frozen rule, budget or current (possibly amended) window. Close or let the deterministic tick create evaluator work. | Frozen closeout with discoverable work. |
| Evaluate | The preregistered independent principal reads criteria and evidence, then submits the supported verdict and gates. | Independent evaluation; missing proof stays missing. |
| Select | Provisioned allocator uses eligibility then fitness. The existing owner path supports bounded allocate/contain for its own cell. | Versioned allocation decision; founder retains irreversible actions. |
| Learn | Propose a cited candidate lesson. Review it through the existing promotion gates. | Candidate with lineage; no silent strategy or memory promotion. |

Budget units must agree across constitution, cell, experiment, work and reservation.
`time` means whole seconds; `tries` and `cycles` are whole counts; money uses its
declared currency and minor units. Offer prices remain money even when an internal
experiment budgets time or tries. A unit mismatch is a configuration discrepancy:
read current policy and propose the exact founder `constitution.amend` if needed.
Do not use operator SQL, counterfeit a currency, or alter a policy to force a bet
through a rejected start.

The generic architecture allows local adaptation within approved boundaries.
Founder-specific restrictions can be narrower. For a founder's own commercial work,
preserve the current exact-packet/version gates for external messages, CRM writes,
spend, hosting and client-data use. A work lease, this guide or a Git maintenance
request does not replace those approvals. A draft or reported historic send is not
a verified execution receipt. 

## Retry, stop and hand off

For an uncertain mutation response, retry the exact payload and idempotency key.
After a revision conflict, read and reconcile current state before submitting a new
intent/key. Stop on identity mismatch, lost lease, containment, budget exhaustion,
missing required approval or unsupported evidence.

After a mutate that advances cell, run, evidence, lease or tries, **stop** or
read back via `company.describe` / `cell_records`. The ledger write is the
handoff; the next force reads MCP. Do not DM a peer, SendToAgent, or use Grok
chat for routine Sense↔Act↔Bound handoffs. Grok chat is not the ledger.
Agent-to-agent chat is out-of-band: founder-facing exceptions, true blockers,
and digests the founder asked for. Channel membership is not a work queue.

That rule is force-peer quietness only. External entities — the founder, other
humans and any non-force agent — stay on normal chat. They are
not ANC ledger peers. When an external is waiting, acknowledge, do the work
(MCP when applicable), and wake or reply. Do not collapse “no routine peer
DMs” into silence toward everyone.

If you must leave a durable note (founder-facing exception or founder-side summary),
carry the relevant non-secret record IDs, revisions, observed time, unresolved
condition and next responsible *force*; never include credentials or lease
tokens. That note is not the handoff — MCP already is.

## Paired Grok ANC effector template (on principal provision)

When a principal is provisioned, the paired Grok bot assumes ANC/MCP holds
truth. Apply this host-side template without changing the published v1 agent
definition:

1. **MCP is the ledger.** After identity activation, operate from
   `company.describe` and `protocol.describe`. Do not treat chat history, channel
   membership, or another bot's recollection as work state.
2. **No SendToAgent for routine Sense↔Act↔Bound handoffs.** Write the ledger,
   then stop (or read back). The next force discovers the write from MCP.
3. **External/host chat stays normal.** Force-peer silence is not silence
   toward the founder or other humans. Acknowledge, do the work, and
   reply when they are waiting.
4. **Surface to the founder only at gates:** sign (every external send), pause,
   ICP change, budget, and activation mismatch.
5. Bound, Sense, and Act remain distinct learning loops. Sense does not debit
   tries; Act complete/fail does. A founder-signed external send is recorded
   with owner-signed `experiment.record_send` and spends exactly one try.
   Draft Act work is not a send. Do not mint an eight-bot org chart.

Provisioning copy and host activation are kept out of this public copy.

`cell.scale`, `cell.unpause` and `learning.promote` remain blocked capabilities in
this beta. Specifically, transitions to `scaled` and `paused` to `active` are not
implemented safely yet, and learning promotion still lacks the complete gate.
Do not retry those paths under other roles or invent substitute SQL. The seven
signed agents and four routines remain the architecture; immune is substrate
policy, and advisory strategy synthesis is founder-owned.

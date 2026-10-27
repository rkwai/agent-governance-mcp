# Phase 1 Company Protocol

Status: executable v1 contract  
Schemas: [`../schemas`](../schemas)

This protocol lets separately authenticated hosts operate one simulated offer cell through MCP. The host supplies intelligence and scheduling. The server is the physics engine: identity, authority, state, budgets, evidence, and auditability. Bound, Sense, Act, Transform, Maintain, Defend, Select, and Continue are persistent forces, not roles or chat personas.

Normative words `MUST`, `MUST NOT`, `SHOULD`, and `MAY` describe protocol requirements.

## Protocol invariants

1. The server MUST derive the principal, company, roles, and effective capabilities from trusted authentication and server-owned grants.
2. Agent registration MUST NOT grant a role or capability.
3. Every mutation MUST use an idempotency key and expected aggregate revision.
4. A successful replay with the same principal, operation, key, and request digest MUST return the original result without another mutation or charge.
5. Reusing a key with a different request digest MUST fail.
6. A stale revision MUST fail without partial writes.
7. Work authority MUST end when its lease expires, is released, or is revoked.
8. Experiment registration MUST freeze its evaluable protocol. Metrics, thresholds, budget unit, evaluator, and offer lineage MUST remain immutable after registration. Owner-signed `experiment.amend` MAY extend a running window, revise stop-rule, treatment, or hypothesis window text, change `budget_amount_minor` through the reservation ledger, or set an experiment-scoped `max_external_messages` override when `founder_decision_ref` is recorded; it MUST NOT retrofit success or failure criteria or amend the constitution. Defend judges the clock against `window_ends_at`, not dates in hypothesis prose. Other material changes require a new experiment.
9. An executor principal MUST NOT be the sole evaluator of its experiment (Defend). The producer MUST NOT clear its own claims.
10. Eligibility gates MUST run before exploitation ranking or allocation.
11. Evidence MUST be append-only and attributable to a principal and agent run.
12. A stop-rule or policy violation MUST block new side effects in its affected scope.
13. Every committed mutation MUST emit a domain event and a content-minimized audit event in the same transaction.
14. The server MUST NOT invoke a model or store hidden reasoning.
15. The eight natural forces MUST be advertised by `protocol.describe` as physics, not as a persona list. Hosts and labeled definitions are optional effectors inside those forces.
16. Sense-class work (`sense_market`) MUST NOT increment experiment try/budget `spent_minor` on complete, fail, or overdue closeout.
17. Act-class work complete or terminal fail MUST debit the experiment reservation when the work carries a positive side-effect cost. Owner-signed `experiment.record_send` MUST debit exactly one try for each recorded founder-signed external send. A draft work completion is not a send.
18. Transform MUST keep unmetabolized activity distinguishable from fitness. Select MUST read fitness, not activity counts.
19. After a mutate that advances cell, run, evidence, lease, or tries, the default next step MUST be stop or read-back via `company.describe` / `cell_records`. Shared MCP state is the force handoff. Hosts MUST NOT treat peer DM, agent chat, or SendToAgent as the routine Sense↔Act↔Bound handoff. Agent-to-agent chat is out-of-band (founder-facing exceptions, true blockers, founder-requested digests). Channel membership is not a work queue. Grok chat is not the ledger.
20. Force-peer silence MUST NOT collapse into silence toward everyone. External entities — the founder, humans, and any non-force agent — remain on normal host chat. When an external is waiting, the host effector MUST acknowledge, do the work (MCP when applicable), and wake or reply.

## Natural forces

`protocol.describe` MUST return `natural_forces` as substrate physics:

| Force | Law the server enforces |
|---|---|
| Bound | Server-resolved identity, constitution, fail-closed grants; caller fields are never authority |
| Sense | `sense_market` and Sense observation writes MUST NOT increment `spent_minor` |
| Act | Side-effecting work complete/fail MUST debit the experiment reservation when cost is positive. Owner-signed `experiment.record_send` MUST debit exactly one try per recorded external send |
| Transform | `activity_evidence_count` vs `fitness_evidence_count`; unmetabolized activity is not fitness |
| Maintain | `ai-nature-tick` expires leases and fails overdue open work |
| Defend | Evaluator ≠ executor; producer cannot clear its own claims |
| Select | Eligibility before rank; allocation reads fitness, not activity |
| Continue | Stop-rule closeout, evidence retention, gated `learning.promote` |

The same eight forces operate similarly at organism (offer cell), colony (company), and civilization (founder genome) scales. Labeled definitions and sit-the-seat principals are optional effectors inside the forces. They MUST NOT be advertised as an eight-persona org chart.

Routine force handoffs are MCP writes plus MCP reads. A successful ledger write is the handoff; the next force discovers work from `company.describe` / `cell_records`, not from a peer DM. Channel membership MUST NOT be treated as a work queue.

External/host chat remains normal and is distinct from that force-peer silence. Hosts MUST acknowledge, do the work, and reply when the founder or another external entity is waiting.

## Identity, definitions, and grants

An agent definition is immutable configuration validated by `agent-definition-v1.json`. It declares a purpose, instructions, compatible roles, and capabilities that the runtime may need. Its capability declaration is an enforced upper bound, never a permission grant.

An authenticated agent principal becomes effective through server-owned identity,
instance, run, and grant records:

```text
verified principal
  + active concrete instance bound to definition ID/version, one role, and optional cell
  + active revocable agent run
  + active company/resource-scoped role grant
  = effective capabilities for this call
```

The effective set is the intersection of definition-declared capabilities, stored
instance allowed actions, active grants, operation policy, target cell, resource
state, and lease scope. A hosted identity also requires the exact immutable
definition projection to exist and match its instance version. Empty intersection or
a missing/mismatched binding fails closed.

The operating path uses separate principals for cell owner, specialist, and evaluator. Those principals submit to forces: owner effectuates Bound (ICP and targets) and MUST NOT impersonate Select; specialist work splits into a Sense effector path (`sense_market`: lists, enrich, and packets within Bound's ICP/target frame; MUST NOT debit tries) and an Act effector path (`scout_market`, sales, delivery: outbound / try-spend from Sense-ready packets only; the founder still signs every external send); evaluator effectuates Defend. Sense is a second specialist effector path, not a new force and not a fourth live principal. Bound, Sense, and Act learning loops MUST stay distinct. Labeled scout, sales, delivery, memory, and allocator definitions are additive effectors mapped onto existing roles. Owner-led allocate/contain does not require an allocator principal and is not portfolio Select. Immune remains leftover configuration, not a standing chat agent. The files under `agents/` are data only; they do not contain executable code, provider selection, or credentials. Do not mint eight force-personas.

### Local stdio authority

The local entry point requires trusted process configuration:

| Environment variable | Meaning |
|---|---|
| `AI_NATURE_PRINCIPAL_ID` | Fixed principal ID for every request on this stdio process. |
| `AI_NATURE_COMPANY_ID` | Fixed company scope for that principal. |
| `AI_NATURE_ROLE_GRANTS` | Non-empty JSON array of unique role names, such as `["cell_owner"]`. |
| `AI_NATURE_DATABASE_URL` | PostgreSQL URL; falls back to `DATABASE_URL`. |
| `AI_NATURE_LOCAL_MODE` | Set to `1` to opt into the in-memory test store when no database URL is present. |

The startup process converts those values into a `VerifiedPrincipal` through the local test authenticator. Tool payloads cannot change the binding. Without a database URL, startup fails closed unless `AI_NATURE_LOCAL_MODE=1` explicitly selects ephemeral storage. This identity mechanism is for local Phase 1 execution only and MUST NOT be exposed as hosted authentication.

Hosted Streamable HTTP uses the official MCP SDK authentication boundary. Selecting `SDKAccessTokenResolver` also requires configured MCP `AuthSettings` and a `TokenVerifier`; the resolver consumes only the SDK-verified access-token context and never parses a raw payload or unverified authorization header.

The standalone ASGI factory is `ai_nature_company.http_app.build_http_app`. Its public issuer and resource URLs MUST use HTTPS; TLS MAY terminate at the deployment ingress. The resource URL includes the exact MCP path, for example `https://substrate.example.com/mcp`. That configuration exposes RFC 9728 metadata at `/.well-known/oauth-protected-resource/mcp` and includes its URL in unauthenticated `WWW-Authenticate` challenges. The transport runs stateless JSON responses by default so replicas do not require sticky sessions.

The same process MAY also serve conservative path aliases at `/mcp/<slug>`, where `<slug>` is `[a-z0-9]+(?:-[a-z0-9]+)*`. Each full URL is a distinct OAuth protected resource. Metadata and `WWW-Authenticate` for a request to `/mcp/<slug>` MUST advertise that URL, not the primary `/mcp`. Identity resolution MUST exact-match the request's full resource URL to the client's primary `resource_url` or an active `anc_oauth_client_resource_aliases` row of the same client. Path aliases MUST NOT mint a new OAuth client and are not extra Railway services. Host allowlists remain host-based; a path is not a host. Empty slugs, `..` segments, and non-conservative slugs MUST fail closed.

Hosted authentication has two explicit dependencies. The injected MCP `TokenVerifier` validates the raw bearer token and emits an `AccessToken` whose `subject` is the stable external identity (Supabase `sub`). A separate injected token authenticator maps only that verified token context to the current server-owned principal, grants, binding, and agent run. Every hosted agent instance MUST have a distinct subject/principal binding. The same OAuth connector `client_id` MAY be shared by several agents; it never selects application authority by itself because resolution uses the exact issuer, subject, and client tuple.

The Supabase-facing transport requires the standard `openid` scope by default. Supabase OAuth does not issue application-specific capability scopes, so MCP tool authority MUST continue to come from the live server-owned binding and policy checks rather than from OAuth scope names.

Every authenticated MCP tool invocation MUST durably append a content-minimized
`received` record before business dispatch and exactly one `succeeded`, `denied`,
or `failed` terminal record. Both records carry only server-derived subject,
principal, instance, definition, role, cell, run, request, and digest fields. Raw
bearer tokens, arguments, results, prompts, and hidden reasoning MUST NOT enter this
call-attribution ledger.

## Mutation envelope

All write tools accept the same outer fields alongside a typed operation payload:

```json
{
  "idempotency_key": "client-stable-non-secret-key",
  "expected_revision": 3,
  "correlation_id": "0191c2e1-52a1-7c22-a6d2-f50b73f635dc",
  "operation_intent": "experiment.start",
  "payload": {}
}
```

Constraints:

- `idempotency_key` is unique within verified principal and operation scope.
- `expected_revision` is the aggregate revision read by the caller, not a company-wide lock.
- `correlation_id` links a workflow but provides no authority.
- `operation_intent` is a finite operation name and MUST match the called tool.
- the payload is validated against the operation contract before authorization or mutation.

The response is bounded:

```json
{
  "operation_id": "0191c2e4-3dc3-7bde-bd19-87d15a7cb401",
  "prior_revision": 3,
  "new_revision": 4,
  "event_ids": ["0191c2e4-43ad-72a8-9a54-65a4a626f81c"],
  "state": {},
  "audit_id": "0191c2e4-47dc-762d-a460-0260d41395c6",
  "replayed": false
}
```

The stored idempotency result includes the request digest and exact response. Authentication failures are not cached as successful idempotent results.

## MCP surface

The Phase 1 surface is intentionally small. Read operations return bounded projections; they do not mutate last-seen markers.

| Area | Operations | Required behavior |
|---|---|---|
| Bootstrap/discovery | `company.bootstrap`, `company.describe`, `constitution.get`, `constitution.amend`, `protocol.describe` | Bootstrap once under server-resolved founder authority; return only caller-visible company state, active versions, limits, and effective capabilities. `constitution.amend` is founder-only: it writes `constitution.activated` as the next version, supersedes the prior active constitution, and realigns company/stage/cell constraint ledgers to exactly one unit. |
| Agents | `agent.register`, `agent.get`, `agent.list` | Register a definition binding without creating a grant; preserve definition version attribution. |
| Work | `work.create`, `work.claim`, `work.heartbeat`, `work.complete`, `work.fail`, `work.release`, `work.cancel` | Enforce outcome contract, capability eligibility, one live lease, expiry, attempt limit, and replay safety. Owner-signed `work.cancel` closes never-claimed work on the caller's own cell with a required reason; it refuses claimed or leased work and other cells. |
| Cells | `cell.create`, `cell.get`, `cell.list`, `cell.transition`, `cell.amend` | Enforce lifecycle, company scope, owner, budget, and optimistic revision; fail `paused` to `active` and all `scaled` transitions closed until their required checks can be atomically bound. Owner-signed `cell.amend` revises objective text on the caller's own active non-terminal cell with a required `founder_decision_ref`; it cannot change budget, stage, owner, `authority_policy`, or constitution. Prior objective remains in `amendment_history`. `cell.transition` to `killed` is founder-signed, or owner-signed with a required `founder_decision_ref` on a quiescent cell (no open experiments, no open or claimed work); either way it ends the cell's running agent runs in the same transaction. |
| Maintenance | `maintenance.run` | On-demand only, never scheduled. Founder (company or one cell) or cell owner (own cell). Report-only by default; `end_runs_on_killed_cells` ends runs still running on killed cells, with an idempotency key and an audit row listing `ended_agent_run_ids`. |
| Offers | `offer.register`, `offer.mutate` | Preserve immutable versions and parent lineage; mutation creates a child. |
| Experiments | `experiment.register`, `experiment.start`, `experiment.close`, `experiment.amend`, `experiment.record_send` | Freeze protocol, atomically reserve budget, enforce stop state, and close into evaluation. `experiment.close` MAY record optional `reason` and `founder_decision_ref`; they persist on the experiment and audit trail and appear on `company.describe` / `audit.list`. Callers that omit those fields keep working. Owner-signed `experiment.amend` extends a running window, revises stop-rule, treatment, or hypothesis window text, changes try-budget amount through the reservation ledger, or sets an experiment `max_external_messages` override with a required `founder_decision_ref`; it cannot retrofit metrics, thresholds, budget unit, or evaluator. Defend judges the clock against `window_ends_at`. Owner-signed `experiment.record_send` records one founder-signed external send and debits exactly one try; it is idempotent per `send_id` and refuses double counting. A process-owned `ai-nature-tick` clock can close a due experiment without a chat session; it applies the current window/budget/stop rules only and never invokes a model. |
| Evidence | `evidence.append`, `economics.append` | Preserve provenance, time, lineage, principal, run, and source authority. |
| Evaluation | `evaluation.submit`, `gate.evaluate` | Verify frozen revision and evaluator independence; produce reproducible gate results. A count-type criterion MAY be recorded as `failed` with empty `evidence_ids` when `observed_value` is 0 and the ledger has no qualifying fitness evidence for that experiment. Passed outcomes still require evidence. An empty-evidence failure MUST be refused when qualifying fitness evidence exists. |
| Allocation | `allocation.propose`, `allocation.commit` | Separate proposal from commitment and record constitution, rule, inputs, gates, rank, and authority. |
| Learning | `learning.propose`, `learning.promote` | `learning.propose` persists a `candidate` from cited experiment, evaluation, and evidence. `learning.promote` remains fail-closed until replay, transfer test, controlled trial, and rollback bind together. The proposing principal MUST NOT promote. |
| Audit | `audit.list`, `decision.explain` | Return bounded recorded facts, rules, and events; never hidden reasoning. |

An implementation MAY expose fewer tools while a vertical slice is under construction, but it MUST NOT claim Phase 1 completion until the complete acceptance path is executable.

`learning.propose` records a candidate. It MUST NOT claim the lesson is memory, policy, or a skill. `learning.promote` MUST fail closed until the remaining Phase 4 gate artifacts exist, and it MUST reject a self-promote even when a founder later holds the promote capability.

The MCP transport accepts one optional `payload` object per tool and injects verified authority from server context. The serialized request and result are each limited to 1 MiB. An individual persisted domain-event payload is limited to 32 KiB and rejects secret or hidden-reasoning key names. The larger transport bound is not permission to persist arbitrary content. Operation-specific application contracts, not arbitrary caller fields, determine the payload's accepted shape.

## Work and lease semantics

- Creation validates `work-item-v1.json` and records required capability, budget, approvals, maximum attempts, and output contract.
- Claim is an atomic compare-and-set from `open` to `claimed` and creates one opaque lease token with an expiry.
- Only the lease-owning principal and run may heartbeat, complete, fail, or voluntarily release it.
- Heartbeat extends from server time and is capped by policy; caller time is never authoritative.
- Expiry makes the old token unusable and returns unfinished work to `open` if attempts remain.
- Completion records result references and actual cost atomically before closing the lease.
- `work.complete` and a terminal `work.fail` debit the work cost from the running
  experiment reservation group when the work is Act and has a positive budget.
  That moves reserved to spent on the company, cell, and stage ledgers so
  portfolio `spent_minor` reflects consumed tries, time, cycles, or money before
  closeout. Sense-class work (`sense_market`) MUST NOT debit `spent_minor` even
  when a cost is recorded on the work item.
- A duplicate claim or completion with the same idempotency record replays its original response.
- A different worker racing for claimed work receives a conflict without learning the lease token.
- Owner-signed `work.cancel` is the owner path for never-claimed open work on the caller's own cell. `reason` is required and is recorded on `cancellation_history`, the audit event, and a decision projection. Optional `superseded_by` names a replacement work item. Claimed or leased work, missing reason, and other cells fail closed with the field named. Cancel does not debit try spend. Cancelled work is terminal and is excluded from `stale_open_work_count`. Live cell-owner bindings receive `work.cancel` through `CELL_OWNER_OPERATIONAL_TOOLS` without reprovisioning and without loosening `constitution.amend` or `authority_policy`. `work.fail` / `work.release` remain lease-holder operations.

## Bounded bet-context discovery

`protocol.describe` returns the eight `natural_forces` as physics, optional
`effectors` (including sit-the-seat alignment), and the host-neutral
`bet_workflow` contract, including identity activation, read pagination,
preregistration, independent closeout, retries, external-action boundaries,
ledger handoff (`write_ledger_then_next_force_reads`), the distinct
external/host-chat obligation (normal; not force-peer silence), and the
currently blocked paths. Signed cast names, when present, are optional
effectors inside the forces — not the architecture. The companion operator
guide is [`BET_OPERATING_GUIDE.md`](BET_OPERATING_GUIDE.md).

`company.describe` with an empty payload retains its identity/constitution/portfolio
response. With `{"cell_records": {"cell_id": "<uuid>", "kind": "work_item"}}`, it
returns only the requested `cell_records` page. Allowed kinds are `work_item`,
`offer`, `experiment`, `evidence`, `learning` and `experiment_send`; no new
capability is granted. `experiment_send` is the sanctioned owner read-back for
recorded founder-signed sends and includes `decision_id`. `sent_at` on a send is
the founder/Gmail-verified send time and is not cross-checked against evidence
timestamps.
Optional `limit` is an integer 1–50 (default 20), and `after_id` is a UUID or null.
No other fields are accepted in this query. Each record contains `record_id`, its
current mutation-precondition `revision`, and full `state`. For offers, `revision`
is `state.version - 1`, as required by `offer.mutate`; it is not the immutable
projection's storage revision. Other kinds use `state.revision` when present,
otherwise their projection revision. Input references retain their own convention
(an offer reference uses `state.version`). Experiments include a linked
`evaluation` when one exists.

The server derives company and cell authority, filters by authorized cell before
paging, and returns `observed_at` and `next_after_id`. It returns whole records up
to 256 KiB of record data, with continuation rather than content truncation. An
oversized individual record fails explicitly. An agent MUST page to null for a
complete traversal; pages are separate observations, so it MUST refresh relevant
state and use revision preconditions before a mutation. This surface excludes
runtime projections, lease tokens and identity tables. It allows a specialist to
discover eligible work and an independent evaluator to retrieve tick-created
closeout work, frozen criteria and actual evidence without relying on chat history.

## Runtime tick

The substrate does not contain an agent scheduler or model. `ai-nature-tick` is a
process-owned Maintain/Continue clock that applies already-frozen rules:

- Maintain: expire due work leases and reopen or fail the work item;
- Maintain: fail open work whose `deadline_at` is in the past; Act overdue work
  debits any remaining try/time/cycle/money budget against the experiment
  reservation, Sense overdue work does not;
- Continue: close a running experiment whose window, budget, or named stop rule
  has fired;
- Continue / Defend: create claimable `evaluate_experiment` work so an
  independent evaluator can close out without a human chat session being open.

Run it by hand with `AI_NATURE_COMPANY_ID` and `DATABASE_URL` (or
`--company-id`). Production does not schedule this mutating path.

### On-demand maintenance scan (`maintenance.run`)

Nothing is scheduled. State is kept accurate by construction instead: for
example, `cell.transition` to `killed` ends the cell's running agent runs in the
same transaction (see below). The kill result and audit row list every ended
run id; the `cell.killed` event carries `ended_agent_run_count`, at most 100
`ended_agent_run_ids`, and `ended_agent_run_ids_truncated`, so a heavily
populated cell stays killable under the 32 KiB event cap. Identity
provisioning refuses a killed cell, so no successor run can be issued. The Maintain/Continue scan is an explicitly
invoked mutation tool, `maintenance.run`, with the usual mutation envelope
(`idempotency_key`, `expected_revision: 0`, `correlation_id`,
`operation_intent`).

- Authority: a founder, company-wide or for one `cell_id`; a cell owner for its
  own `cell_id` only (company-wide is refused).
- Payload: `{"cell_id"?: string, "end_runs_on_killed_cells"?: boolean}`.
  Unknown fields are refused.
- Default (report only): returns `state.report` (findings plus "would do") and
  changes nothing. Findings are built through a read-only transaction wrapper
  that refuses every write method.
- `end_runs_on_killed_cells: true`: also ends every agent run still `running` on
  a killed cell in scope (status `revoked`, `termination_code: cell_killed`).
  The result and the audit row list `ended_agent_run_ids` and
  `ended_agent_run_ids_by_cell`. Replaying the same idempotency key returns the
  first result; a new key finds nothing left to end.

The report lists findings and what a repair *would do*:

| Finding | Would do |
|---|---|
| `agent_run_expiring` (running run expires within 72h) / `agent_run_expired` (still `running` past `expires_at`) | `rotate_agent_run` (operator rotate; not a substrate action) |
| `agent_run_on_killed_cell` | `end_agent_run` (`maintenance.run` with `end_runs_on_killed_cells`) |
| `work_lease_expired` | `expire_lease_and_requeue_work`, `expire_lease_and_fail_work`, or `expire_lease_and_fail_work_deadline_elapsed` |
| `claimed_work_without_active_lease`, `claimed_work_past_deadline` | `flag_cell_owner` |
| `open_work_deadline_elapsed` (expired unclaimed) | `fail_work_deadline_elapsed` |
| `experiment_close_eligible` (window, budget, or stop rule fired) | `close_experiment_and_open_closeout_evaluation_work` |
| `experiment_awaiting_evaluation` (evaluating, no evaluation) | `open_closeout_evaluation_work` or `flag_independent_evaluator` |
| `active_cell_idle` (active, no open work or open experiments) | `flag_cell_owner` |

Founder-only steps that no live binding can sign (learning 13) are not yet
evaluated. The report marks `sections.founder_signing_paths` with status `todo`.

**No silent truncation.** Every source is read to exhaustion: running agent
runs are keyset-paged on `(expires_at, agent_run_id)` through
`anc_list_maintenance_agent_runs_page`, and cell, work-item, experiment, and
evidence projections are keyset-paged by projection id. Safety caps are 10,000
runs and 50,000 projections per type. Each report section (`agent_runs`,
`work_leases`, `experiments`, `idle_cells`, `findings`) carries
`{status, truncated, scanned, cap}`; `status` is `complete` only when nothing
was cut off. Idle-cell findings are suppressed when the work or experiment scan
was truncated. The findings list is capped at 200; `counts`, `counts_by_cell`,
and `total_findings` cover every finding. A cell-scoped call sees only that
cell's findings and scan counts. `protocol.describe.runtime_tick.on_demand`
advertises this contract; `runtime_tick.scheduled` is `null`.

"Own cell" in ANC means the caller's agent binding covers the cell. It does not
mean the cell's `owner_principal_id`. A cell's `owner_principal_id` may be the
founder while the acting cell owner is a bound agent.

`company.describe` and `cell.get` expose remaining budget by
ledger, `spent_minor`, experiment stop state, pause/kill/close eligibility,
`activity_evidence_count`, `fitness_evidence_count`, and `stale_open_work_count`.
Activity units such as `packet_sent` and Sense units such as `demand_hint` are
unmetabolized. Select reads fitness, not packets.

## Experiment and budget semantics

Registration validates `experiment-v1.json` and freezes hypothesis, offer version, population, metrics, thresholds, stop rules, window, actions, executors, independent evaluator, and maximum budget. The application rejects an evaluator principal that appears in the executor set; JSON Schema cannot compare those values across fields.

`work.cancel` is an owner-signed exception for never-claimed open work on the caller's own cell. The cell-owner principal MAY move that item to `cancelled` when a non-empty `reason` is recorded. Optional `superseded_by` is a work-item UUID. `idempotency_key` and `expected_revision` follow the mutation contract. Claimed or leased work MUST be refused with the field named (`status` or `lease_id`). Work on another cell MUST be refused. Missing or blank `reason` MUST be refused with `reason` named. Replay with the same key and digest MUST return the original result. Cancel MUST NOT debit try spend and MUST NOT rewrite prior history. Evaluators, specialists, and the founder MUST be refused. Live cell-owner bindings receive `work.cancel` through `CELL_OWNER_OPERATIONAL_TOOLS` without reprovisioning and without loosening `constitution.amend` or `authority_policy`.

`cell.amend` is an owner-signed exception for the caller's own active, non-terminal cell. The cell-owner principal MAY revise `objective` text. `founder_decision_ref` is required and is recorded on the amendment, audit event, and decision projection. The prior objective remains in `amendment_history` and is visible on `audit.list`, `decision.explain`, `company.describe`, and `cell.get`. Budget, stage, owner, `authority_policy`, and constitution MUST be rejected if supplied. Evaluators, specialists, and the founder MUST be refused. Live cell-owner bindings receive `cell.amend` through `CELL_OWNER_OPERATIONAL_TOOLS` without reprovisioning and without loosening `constitution.amend` or `authority_policy`.

Owner-signed kill is an exception to founder-only `cell.transition` to `killed`. The cell-owner principal MAY kill an `active` or `paused` cell its binding covers only when `founder_decision_ref` is supplied, and only when the cell has no `registered`, `running`, or `evaluating` experiments and no `open` or `claimed` work. `reason` is optional. The kill records a `cell.kill` decision with `kill_authority: cell_owner_founder_decision_ref`, the ref, and the reason; they are visible on `decision.explain` and `audit.list`. Without `founder_decision_ref` the kill remains founder-only. Specialists, evaluators, allocators, and immune MUST be refused. A founder kill MAY carry optional `reason` and `founder_decision_ref`, which persist on the audit trail. Scaling stays founder-only and fails closed.

Every kill path (founder and owner-signed) ends all of the cell's `running` agent runs in the same transaction as the cell transition (status `revoked`, `termination_code: cell_killed`, through the runtime-only `anc_end_cell_agent_runs` function). The `cell.killed` event payload, the response `state.ended_agent_run_ids`, and the `cell.transition` audit row's `ended_agent_run_ids` list them. A refused kill ends nothing.

`experiment.amend` is an owner-signed exception for a running experiment in the caller's own cell. The cell-owner principal MAY extend `window.ends_at` (the new end MUST be later than the current end and later than now), MAY revise `stop_rules`, `treatment`, or hypothesis window text, MAY change `budget_amount_minor` through the same reservation/ledger path used at start, and MAY set an experiment-scoped `max_external_messages` override. At least one of those fields MUST change. `founder_decision_ref` is required and is recorded on the amendment, audit event, and decision projection. Prior window, stop rules, treatment, hypothesis, budget amount, and message-cap values remain in `amendment_history` and are visible on `audit.list`, `decision.explain`, and `company.describe`. The budget unit MUST stay frozen; only the amount may change, and the new amount MUST NOT drop below already spent or exceed the constitution `experiment_limit_minor`. `max_external_messages` MAY rise or fall, but MUST NOT exceed the (possibly amended) try-budget amount and MUST NOT drop below the recorded send count. The override MUST NOT amend the constitution or any other `risk_policy` value. Metrics, success/failure thresholds, the `budget` object, evaluator, cell, and offer lineage MUST be rejected if supplied. Evaluators and specialists MUST be refused. The Maintain/Continue tick and Defend `evaluation.submit` judge the clock against the amended `window_ends_at`, not dates embedded in hypothesis prose. Live cell-owner bindings receive `experiment.amend` through `CELL_OWNER_OPERATIONAL_TOOLS` without loosening `constitution.amend` or `authority_policy`.

`experiment.record_send` is the sanctioned Act debit for one founder-signed external send. The cell-owner principal MAY record a send against a running experiment when the cell is active. The mutation MUST debit exactly one try from the experiment reservation, MUST be idempotent per caller-supplied `send_id`, and MUST refuse a second `send_id` for the same experiment, target, channel, and `sent_at`. It MUST enforce the experiment `max_external_messages` override when present, otherwise the stored constitution `max_external_messages_per_experiment`, before debiting: a send that would exceed the cap MUST fail closed with an explicit remaining count, and a send under the cap MUST NOT be blocked by the cap. The remaining count is visible on `company.describe` / `cell.get` experiment views as `external_messages_remaining`. `founder_signoff_ref` and an existing same-cell, same-experiment `evidence_id` are required. `sent_at` is the founder/Gmail-verified send time and MUST NOT be cross-checked against evidence `observed_at` or other evidence timestamps. Completing draft Act work (for example `scout_market` with `actual_cost` 0) MUST NOT count as a send. Sense work MUST NOT debit. Evaluators, specialists, and the founder MUST be refused. The send, decision, and audit event MUST remain inspectable via `company.describe` `cell_records` kind `experiment_send` (includes `decision_id`) and cell-scoped `audit.list` for live cell-owner bindings. Live cell-owner bindings receive `experiment.record_send` and cell-scoped `audit.list` through `CELL_OWNER_OPERATIONAL_TOOLS` without reprovisioning. Unexpected failures MUST return a structured `{code, message}` refusal, never a bare unexpected-exception string.

### Units of account

A company constitution `budget_policy`, cell ledger, experiment budget, work
budget, and reservation/spend row MUST use exactly one unit of account:

| Unit | Wire `unit` | Canonical integer (`*_minor` / `amount_minor`) | Currency |
|---|---|---|---|
| Money | `money` (default when omitted) | ISO 4217 minor units | Required ISO 4217 code |
| Time | `time` | Whole seconds of attention or wall-clock | MUST NOT be present |
| Cycles | `cycles` | Whole compute/work cycles | MUST NOT be present |
| Tries | `tries` | Whole attempts | MUST NOT be present |

Time has one explicit scale: **seconds**. There is no minutes or hours ledger
scale. Internal closed beta is expected to budget in `time` and `tries`; both are
first-class units, not extensions of money.

Existing USD constitutions that omit `unit` and declare `currency: "USD"` remain
valid money policies and MUST still load. Offer `price` stays commercial money and
is not a constraint-ledger quantity. `TRY` is Turkish lira (money). Attempts use
unit `tries`.

One ledger, one unit. A reservation in tries MUST NOT consume a money ledger. A
single budget row MUST NOT mix time with USD. Company, cell, and stage rows in
one reservation group MUST share the same unit (and the same currency when the
unit is money). Mixing units fails closed.

Money still uses the Phase 1 two-decimal ledger representation (major units in
storage, minor units on the wire). Time, cycles, and tries persist the same
integer that appears on the wire.

Start performs one transaction:

1. verify `registered` state and expected revision;
2. verify principal authority and active enclosing cell;
3. check approvals, policy, and the current constitution;
4. reserve the exact experiment budget against company, cell, and exploration/exploitation ledgers;
5. transition to `running` and append events;
6. save the idempotent response.

If any step fails, none are committed. Actual costs consume the reservation. Unused
quantity is released only when the experiment reaches a terminal state or an
authorized compensation occurs.

The company, cell, and global stage rows are constraint-ledger mirrors of one
logical hold. Each member carries the same amount and a shared reservation
group ID; the three member amounts MUST NOT be summed as three costs. The
constitution partitions the company limit into a global exploration ledger and an
exploitation ledger. A hold succeeds only when every applicable ledger has capacity,
and reserve, commit, release, and replay update the complete group atomically.

## Evidence and economics

Evidence validates `evidence-v1.json`. Source authority is explicit:

- `authoritative`: direct adapter receipt from the system of record;
- `verified`: independently checked against a source artifact;
- `attested`: a named human or agent assertion;
- `inferred`: a reproducible derivation from cited events.

Cash, refunds, and product usage SHOULD use authoritative adapters when available. A manual entry cannot claim authoritative provenance. Corrections, supersessions, and retractions append a new event whose lineage points to the prior event. Current projections apply the lineage; the event history remains unchanged.

## Evaluation and allocation separation

`evaluation.submit` validates `evaluation-v1.json` and then performs a server-side equality check between the verified evaluator principal and every recorded executor principal. JSON Schema cannot express this cross-record invariant; the application MUST enforce it.

A count-type criterion whose observed value is 0 MAY be submitted as `failed` with empty `evidence_ids`. The server MUST verify that claim against the ledger: when `evidence_ids` is empty, the observed count MUST be 0 and there MUST be no qualifying fitness evidence for the experiment. Passed outcomes still require at least one evidence id. Failures on non-count criteria keep their current evidence rules. An empty-evidence failure MUST be refused when qualifying fitness evidence exists. After a successful submit the experiment transitions exactly as any other failed evaluation.

`experiment.close` MAY accept optional `reason` and `founder_decision_ref`. When supplied they persist on the experiment projection, the close event, and the audit trail, and they are visible on `company.describe` and `audit.list`. Omitting them remains valid.

For `gate.evaluate`, `allocation.propose`, and `allocation.commit`, the application resolves the target projection inside the authenticated company transaction, takes the cell ID from that trusted record, and requires the active agent binding to cover that cell. A caller-supplied target ID locates a candidate record; it never expands a differently bound evaluator or allocator's scope.

`fund` requires a positive amount in the company's unit of account. `maintain`, `clone`, and `mutate`
require a null amount and are evaluated with zero requested allocation; they never
create a funding reservation. Their stage still selects the canonical exploitation
or exploration ranking formula.

Manual containment accepts only `pause` or `kill`, containment stage, null
amount, and no evaluation ID. `pause` may use a `founder_authorized` or
`cell_owner_authorized` basis containing a concise summary. `kill` still requires
`founder_authorized`. For founder authorization the server requires the founder-only
approval capability at the current company root, checks the principal against the
active constitution, and stamps the authorizing principal and server time. For
owner authorization the server requires a `cell_owner` principal whose binding
covers the target cell and stamps that principal and server time. A company-wide
non-founder grant cannot be combined with a narrow founder grant to pass the
founder check. Caller-asserted policy and stop-rule bases fail; verified
policy/hard-stop evidence uses the automatic immediate-pause path until a later
protocol adds typed, independently verifiable policy and stop-rule basis records.

Containment records the complete seven-gate set without fabricating passes. Its
eligibility comes from the verified recorded basis, its amount is null, it bypasses
funding ledgers, and its rank is exactly `not-ranked` `0/1`. The basis is bound into
the allocation input digest and retained in the decision projection and proposal,
commit, and cell-transition events. Proposal and commit each require the gate
decision constitution ID/version to remain active. Commit of `pause` requires
bounded allocate/contain authority from the cell owner of that cell or a deferred
allocator; commit of `kill` requires founder-only irreversible authority. It
atomically updates the allocation, decision, and exact cell revision without a
reservation. A verified human founder may have a null run on the irreversible
decision; principal and server time remain mandatory.

All other allocation commits require a current evaluation, exact target revision,
active constitution, complete eligible gate set, and the deterministic score inputs
described in `FITNESS_AND_ALLOCATION.md`. A positive funding commit also requires
live company, cell, and stage capacity and atomically reserves all applicable
ledgers. Every commit records a concise rationale summary, not chain-of-thought.

## Error contract

Errors are typed and content-minimized. The Phase 1 MCP error object contains a stable `code` and safe `message`. Correlation is available through the mutation/audit record after a committed operation. Expected codes include:

| Code | Meaning | Retry guidance |
|---|---|---|
| `unauthenticated` | Principal could not be verified | Re-authenticate; do not blind retry. |
| `run_expired` | Token and subject binding exist, but the caller's agent run is past `expires_at` | Renew the run during an operator window; do not treat this as an unknown identity. |
| `run_revoked` | Token and subject binding exist, but the caller's agent run is revoked | Containment or rotation is required; do not retry the same run. |
| `run_suspended` | Token and subject binding exist, but the caller's agent instance is suspended | Resume only after an operator un-suspend; do not infer other principals. |
| `run_inactive` | Token and subject binding exist, but the caller's run is otherwise unusable | Inspect the named run; do not treat this as an unknown identity. |
| `forbidden` | Effective capability or scope is absent | Requires a grant or different principal. |
| `invalid_operation` | Payload or requested transition violates the contract | Correct payload or intent. |
| `conflict` | Revision, idempotency claim, or concurrent mutation conflicts | Re-read and reconcile before retrying. |
| `invalid_lease` | Lease is absent, mismatched, released, or expired | Reclaim before continuing. |
| `budget_exceeded` | Reservation or spend exceeds a limit | Reduce scope or obtain founder authority. |
| `separation_of_duties` | Evaluator or allocator conflicts with an execution duty | Use a separate authorized principal. |
| `not_found` | Resource is absent or not caller-visible | Do not infer cross-company existence. |
| `internal_error` | Unexpected substrate failure; the full traceback is logged server-side | Retry the exact mutation with the same idempotency_key after the fault is cleared. |
| `payload_too_large` / `result_too_large` | MCP envelope exceeds the 1 MiB transport bound | Reduce or paginate content. |

## Reconstruction

Every acceptance-path decision must be reconstructable from:

- active constitution and rule versions;
- immutable agent definition and principal binding;
- work item, lease, and agent-run receipts;
- offer and experiment revisions;
- evidence/economic lineage;
- evaluation and gate results;
- allocation input snapshot;
- domain and audit events.

Reconstruction explains recorded inputs and rules. It does not require or permit storage of a model's hidden reasoning.

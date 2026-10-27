# Technical Approach: AI Nature Company MCP Substrate

Status: **eight natural forces are executable substrate physics**
Date: **2026-09-13**
Alignment target: [`README.md`](../README.md)
Founding philosophy: [`FOUNDING_CONVERSATION.md`](FOUNDING_CONVERSATION.md)
Frozen Phase 1 contracts: [`PROTOCOL.md`](PROTOCOL.md), [`ARCHITECTURE_BOUNDARY.md`](ARCHITECTURE_BOUNDARY.md), [`STATE_MACHINES.md`](STATE_MACHINES.md), [`FITNESS_AND_ALLOCATION.md`](FITNESS_AND_ALLOCATION.md)

This document maps Bound, Sense, Act, Transform, Maintain, Defend, Select, and Continue onto MCP tools and state. The MCP is the physics engine. Hosts and labeled definitions are optional effectors inside those forces — not an eight-persona org chart. Live sit-the-seat owner / specialist / evaluator bindings stay valid and submit to forces. Bound (owner) determines ICP and targets. Sense is a second specialist effector path for `sense_market` research — not a new force. Act (hunter) is try-debit outbound from Sense-ready packets; the founder still signs every external send. Owner-led allocate/contain remains a temporary single-cell path and does not impersonate Select. Immune, keeper, and strategy stay un-minted. Routine sit-the-seat handoffs are MCP writes plus reads; Grok chat is not the ledger.

## 1. Architecture boundary

Generative reasoning stays in the user-selected AI host. The MCP server is the company's deterministic pheromone environment and gate layer. It does not think, schedule agents, or invoke a model.

```text
host reasons, plans, writes copy, judges relevance, proposes mutations
→ authenticated MCP call as a server-resolved principal
→ MCP checks identity, grant, revision, budget, lease, policy, and stop state
→ append-only evidence / work / allocation / audit is committed atomically
→ bounded structured state returns
→ host stops, or reads back via company.describe / cell_records
```

The ledger write is the force handoff. The next force reads MCP. Do not follow a successful mutate with a peer DM, SendToAgent, or agent chat. Grok chat is not the ledger. Agent-to-agent chat is out-of-band: founder-facing exceptions, true blockers, and digests the founder asked for. Channel membership is not a work queue.

This is the same host-intelligence / server-policy split as `essence-protocol`. The host interprets the market. The substrate validates, persists, retrieves, and enforces contracts. Particles move; the physics does not.

### The host owns

- agent creation, model selection, inference, scheduling, and retry of model calls;
- market interpretation, offer ideation, copy, and planning;
- relevance judgments and root-cause hypotheses;
- advisory strategy synthesis for the founder (not a standing persona);
- application of retrieved, already-promoted learning.

### The MCP substrate owns

- verified human, service, runtime, and agent identities (Bound);
- versioned agent definitions and principal bindings (Bound);
- durable work items, leases, handoffs, and run receipts (Act / Maintain);
- constitution and policy versions (Bound);
- offer, experiment, allocation, and skill state machines;
- revision preconditions and idempotency;
- budgets and spend reservations (money, time in seconds, cycles, tries) — Sense does not debit tries; Act does;
- append-only evidence with provenance, decay fields, and lineage (Sense / Transform);
- producer / evaluator separation (Defend);
- deterministic eligibility, stop, and immune gates (Select / Bound / Continue);
- append-only audit events;
- bounded retrieval and export;
- rollback and compensating-action records (Continue).

### Forbidden inside the server

- model inference, model routers, provider credentials, or BYOK;
- an agent scheduler or autonomous control loop (a process-owned tick that only applies frozen stop rules and lease expiry is in bounds Maintain/Continue; see §4);
- hidden chain-of-thought or agent-debate transcripts;
- caller-supplied actor, role, company, cell, budget, or entitlement used as authority;
- silent policy, strategy, or memory mutation;
- unsupported claims that an agent, offer, or experiment succeeded;
- eight standing force-personas or a chatbot society.

The server may calculate a documented deterministic score, enforce a typed contract, and call an approved non-generative adapter. It must never become the centralized intelligence the architecture is meant to avoid.

## 2. The offer cell is the organism

The persistent unit is an end-to-end offer cell, not an agent and not a department. A cell owns the complete loop:

```text
customer → painful job → promise → proof → price → channel
→ sale → delivery → outcome → collection → retention → learning
```

Temporary specialist work happens inside the cell. A cell survives only when it repeatedly creates customer value with attractive, attributable economics.

Do not organize the next increment as a society of chatbots (no immune bot, no keeper bot, no strategy Grok, no eight force-bots). The efficient primitive is one owner per outcome, temporary specialist work, independent verification, and shared evidence.

### Other units the kernel already names

| Unit | Meaning |
|---|---|
| Company | Isolation and authority boundary: one active constitution, members, grants, budgets, integrations, event history. Colony Bound. |
| Agent definition / instance / run | Reusable reviewed configuration; one concrete principal+role+optional cell; one revocable operating window. Registration never grants authority. Effector, not a force. |
| Work item + lease | Durable outcome contract. Authority lives on the expiring lease, not in chat. Sense vs Act is a work-kind force split. |
| Offer version | Immutable genome. Mutation creates a child with parent lineage. |
| Experiment | Preregistered hypothesis, budget, window, thresholds, and stop rule. |
| Evidence event | Append-only observation with provenance. Corrected by a linked event, never edited in place. Transform grammar: activity vs fitness. |
| Allocation | Versioned fund / maintain / clone / mutate / pause / kill. Kill is founder-only. Select. |
| Skill | Promoted reusable capability. Not permanent merely because an agent wrote it down. Continue. |

## 3. Eight forces → MCP law

`protocol.describe` advertises `natural_forces` as physics. Signed cast names remain a convenience list of optional effectors. They are not the eight forces.

| Force | Kernel-now | Must stay true |
|---|---|---|
| **Bound** | Constitution, grants, `company.bootstrap`, fail-closed identity; ICP and targets | Caller-supplied actor/role/cell/budget is never authority. Inside vs outside is server-owned. Bound learns ICP / target / offer adaptation. |
| **Sense** | `sense_market` work; Sense observation units on `evidence.append` | Must not increment try `spent_minor`. Observation is not Act. Lists, enrich, and packets stay inside Bound's ICP/target frame. |
| **Act** | `scout_market`, `sales_step`, `delivery_step`, and other side-effecting work complete/fail; owner-signed `experiment.record_send` | Must debit try/budget spend when cost is positive. Each recorded founder-signed external send spends exactly one try. Act specialist (hunter) sends only from Sense-ready packets. Founder signs every external send. |
| **Transform** | `evidence.append` / `economics.append`; `activity_evidence_count` vs `fitness_evidence_count` | Unmetabolized activity stays distinguishable. Select reads fitness, not packets. |
| **Maintain** | `ai-nature-tick` expired leases and overdue open work | Hygiene, not reflex chat. Sense overdue work still does not debit tries. |
| **Defend** | `evaluation.submit`, `gate.evaluate`; tick-created `evaluate_experiment` | Evaluator ≠ executor. Producer cannot clear its own claims. |
| **Select** | `allocation.propose` / `allocation.commit` + `fitness-v1` | Eligibility before rank. Separate exploration/exploitation ledgers. Owner does not impersonate Select. |
| **Continue** | Experiment closeout, stop rules, `learning.propose`, fail-closed `learning.promote` | Failed experiments stay inspectable. No observation automatically becomes memory. |

### Effectors mapped under forces (not the architecture)

Roles remain permission sets, not personalities. The kernel still has `cell_owner`, `specialist`, `evaluator`, `allocator`, `founder`, plus a leftover `immune` grant and `observer`. Dedicated definition files exist for scout, sales, delivery, and memory. `cell-owner` holds the organism membrane (ICP, targets, offer lineage). `allocator` effectuates Select. `specialist/` remains the generic leased-work definition for live sit-the-seat specialist bindings. Sense is a second specialist effector path (`sense_market`), not a new force or a fourth live principal. Act (hunter) is the try-debit outbound path. `immune/` is leftover config — do not mint a bot from it.

| Effector | Force | Sit-the-seat note |
|---|---|---|
| Sense specialist | Sense (`sense_market`) | Second specialist path. Lists, enrich, packets within Bound ICP/targets. No try debit. Learns list / enrich / door / buyer-identity quality. |
| Market scout | Act (`scout_market`) | Outbound from Sense-ready packets only. Founder signs every send. Learns try effectiveness after Sense-ready — not list volume. |
| Offer designer / cell owner | Bound | Determines ICP and targets. Does not impersonate Select. Learns ICP / target / offer adaptation. |
| Sales | Act | Bookings are not completion. Cannot evaluate. Sense-ready packets only. |
| Delivery | Act | Record overruns. Cannot evaluate. |
| Experiment evaluator | Defend | Evaluator ≠ executor. |
| Memory | Continue | Cannot self-promote. |
| Allocator | Select | Must not execute or evaluate the experiment it funds. |

Routine Bound / Sense / Act hops write the ledger and stop. The next force reads MCP. Do not SendToAgent for those hops. Surface to the founder at sign, pause, ICP change, budget, and activation mismatch.

### Temporary kernel path (do not promote it to Select)

The bound cell owner can propose and commit operational allocate/contain for that cell. That unblocks closed-beta operation without an allocator principal. It is containment of a bound cell, not portfolio ranking.

Founder retains constitution changes, irreversible kill, major capital, high-risk market entry, and promotion of strategy-level mutations. `allocation.commit` of `kill` stays founder-only.

### Capability sketch

Sense and Act effectors should look like today's specialist: `work.claim`, `work.heartbeat`, `work.complete` / `fail` / `release`, `evidence.append`, `economics.append`, discovery reads. They must not evaluate their own experiment or commit allocation. Sense work does not spend tries. Act work does.

Designer / cell owner should keep `offer.register`, `offer.mutate`, `experiment.register` / `start` / `amend`, `work.create`, owner-signed `work.cancel` for never-claimed leftovers on that cell, cell reads, and bounded owner-led contain for that cell only.

Evaluator should keep `evaluation.submit`, `gate.evaluate`, and the ability to claim tick-created closeout work. It must not execute the experiment it judges.

Memory may `learning.propose` a persisted candidate from cited experiment, evaluation, and evidence. `learning.promote` remains fail-closed until attribution, replay, regression, controlled trial, and rollback bind together. The proposer cannot promote.

Allocator should receive `gate.evaluate`, `allocation.propose`, and `allocation.commit` across cells under the constitution. It must not execute or evaluate the experiment it funds.

## 4. Four cell routines → runtime

Faster loops do not wait for slower ones unless they cross a Bound constitution boundary (price floor, company promise, security posture, irreversible kill, major capital, high-risk entry).

| Routine | Cadence | What may change | Forces | Runtime mapping |
|---|---|---|---|---|
| **Reflex** | Minutes to hours | Next observation or world-changing step | Sense, Act | Work items + expiring leases. `sense_market` does not debit tries. `scout_market`, `sales_step`, and `delivery_step` do. **Reflex is leased work, not a cron chat and not `ai-nature-tick`.** |
| **Adaptation** | Daily | Copy, targeting, sequence, workflow parameters | Bound, Transform | `offer.mutate` inside a defined claim boundary by the cell owner. Creates a child version. Cannot cut price below the margin floor or change company promise or security posture. |
| **Selection** | Weekly | Budget, priority, cloning, termination | Select, Defend | Signed controller: allocator + `fitness-v1`. Select reads `fitness_evidence_count`, not activity. Kernel-now: owner-led allocate/contain for one bound cell. |
| **Evolution** | Monthly, or after strong evidence | ICP, offer architecture, business model, strategy | Continue, Bound | Founder. Advisory synthesis may propose; the founder approves genome-level mutation. Not a standing strategy agent. |

### Where the tick belongs

`ai-nature-tick` is a process-owned clock. It is Maintain and Continue:

- expire due leases and reopen or fail the work item (Maintain);
- fail open work whose `deadline_at` is in the past; Act overdue work debits remaining budget, Sense overdue work does not (Maintain);
- close a running experiment whose window, budget, or named stop rule has fired (Continue);
- create claimable `evaluate_experiment` work so an independent evaluator can close out without a human chat session (Continue / Defend).

It never invokes a model and is not an agent scheduler. Tick and cron are not reflex. Reflex happens when a principal claims a Sense or Act lease.

Production schedules nothing. State is kept accurate by construction: `cell.transition` to `killed` ends the cell's running agent runs in the same transaction. The Maintain/Continue scan is the explicitly invoked `maintenance.run` tool (founder, or cell owner for its own cell): report-only by default, and with `end_runs_on_killed_cells` it ends runs still running on killed cells behind an idempotency key and an audit row. See `PROTOCOL.md` § On-demand maintenance scan.

### Topology rule (do not violate in the next increment)

| Task | Shape |
|---|---|
| Sequential, tightly coupled, context-heavy | One capable agent with tools and skills |
| Independent market exploration | Multiple parallel Sense/Act effectors |
| Dynamic environment with local feedback | Decentralized offer cells using shared state |
| Cross-cell resource conflict | Central Select |
| High-stakes or irreversible action | Central Defend and human approval |
| Adversarial evaluation | Independent producer and verifier |

## 5. Evidence and digital pheromones

Effectors coordinate by appending shared state, not by holding meetings. Routine Bound / Sense / Act handoffs are MCP writes plus MCP reads. Agent-to-agent chat is out-of-band; channel membership is not a work queue. Every signal the README requires is already a field on `schemas/evidence-v1.json`, or is documented as review order in `fitness-v1`. Transform classifies envelopes as activity (unmetabolized) or fitness (metabolized). Select reads fitness.

### Already in evidence-v1

| Signed field | Kernel field | Notes |
|---|---|---|
| source | `source_system`, `source_reference` | Plus `provenance_quality`: authoritative / verified / attested / inferred. |
| timestamp | `observed_at`, `recorded_at` | Caller time is not authority for leases or ticks. |
| offer cell | `cell_id` (required); `offer_version_id`, `experiment_id` optional | Company ID is server-derived, not caller authority. |
| strength | `strength` | Typed numeric value ≥ 0. |
| confidence | `confidence_bps` | Integer 0..10000. |
| causal distance | `causal_distance` | `direct` / `near` / `intermediate` / `proxy`. |
| half-life | `half_life_seconds` | Stored. Not yet applied as a decision input. |
| identity | `recorded_by_principal_id`, `agent_run_id` | Server-resolved; payload cannot choose them as authority. |
| lineage | `lineage.relation` | original / correction / supersession / retraction. |

Negative classes already exist as `type` values: `refund`, `complaint`, `churn`, `delivery_overrun`, `manual_exception`, `security_incident`, `policy_violation`, plus `cost`. Cash, usage, outcome, commitment, proposal, meeting, and reply exist as positive/proxy types. Cell and portfolio projections classify those types, plus named observation units such as `packet_sent` vs `payable_covering_conversation`, and Sense units (`demand_hint`, `market_signal`, `observed_behavior`, `world_signal`), as activity or fitness counts. The counts are instrumentation, not a second fitness score. Select reads `fitness_evidence_count`.

Revenue, payment, product-usage, and delivery facts must come from authoritative adapters or explicitly labeled attestations. A manual entry cannot claim `authoritative` provenance.

### Not yet executable (next evidence work)

**Decay.** `half_life_seconds` is required on every event. `fitness-v1` documents a decayed routing formula and forbids using that decayed value as eligibility or capital rank unless a later rule version specifies a deterministic approximation. Phase 1 stores raw inputs and server observation time. The next increment that touches pheromones should apply decay as **review ordering**, not as a silent fitness score.

**Hierarchy as review, not score.** Documented order:

```text
collected contribution profit
> retained usage or renewal
> collected cash
> signed customer commitment
> verified customer outcome
> proposal requested
> meeting attended
> positive reply
> click or page view
```

This order guides provenance review. It must not convert a click into cash evidence or replace `fitness-v1`. Do not add a hidden embedding rank. `evidence-v1` does not yet have dedicated `type` values for click, page view, or collected contribution profit; those rungs are review labels over `reply` / `usage` / `cash` / `outcome` / `economics.append` until a later schema revision adds them.

**Negative evidence as first-class gates.** Types exist. A verified policy violation or frozen hard stop can pause side effects without waiting for portfolio rank. Caller assertions that a policy or stop rule fired are never a containment basis. What still must be added: typed, independently verifiable policy and stop-rule basis records for the remaining negative classes, so refunds, overruns, complaints, and unexpected manual work can trip configured hard stops without an immune chat agent.

**Routing score vs fitness.** A future attention score may be:

```text
strength × confidence × provenance weight × causal-distance weight × exp(-age / half-life)
```

That score prioritizes review. It is not company fitness and must not mix learning credit into exploitation rank.

## 6. Immune is Bound + Defend policy, not a persona

Immune function — verification, security, compliance, fraud, brand, unauthorized action, rollback — is enforced in the substrate:

- server-resolved identity and grants;
- eligibility and safety gates before fitness rank (`fitness-v1` seven gates);
- stop rules frozen on the experiment;
- automatic pause of new side effects when a verified policy or hard stop fires;
- evaluator ≠ executor;
- founder-only irreversible kill and constitution change;
- content-minimized append-only audit.

`agents/immune/agent-definition.json` is deferred Phase 1 configuration. Do not mint a Grok bot, keeper bot, strategy bot, or force-bot to "be the immune system." If a later increment keeps an `immune` grant, it is a permission to inspect audit and request pause — not a standing persona.

## 7. Core operating loop (forces mapped to tools)

```text
BOUND          constitution, grants, fail-closed identity
SENSE          sense_market / observation evidence; no try debit
HYPOTHESIZE    cell owner proposes offer.mutate or experiment.register
PREREGISTER    substrate freezes hypothesis, budget, thresholds, stop rules
AUTHORIZE      server-resolved identity, role, policy, revision, resources
ACT            scout / sales / delivery claim reflex work under a lease; complete/fail spends
TRANSFORM      evidence.append / economics.append; activity ≠ fitness
MAINTAIN       tick expires leases and fails overdue open work
DEFEND         independent evaluator; tick may create the closeout work item
SELECT         allocator + fitness-v1 (kernel-now: owner-led contain, not portfolio Select)
CONTINUE       stop-rule closeout; memory via learning.propose → gated learning.promote
EVOLVE         founder approves genome / constitution / irreversible kill
```

A failed experiment that cheaply kills an expensive false assumption is a good use of exploration capital. It is not rewritten as a commercial win.

## 8. MCP surface the next increment consumes

Names are frozen in [`PROTOCOL.md`](PROTOCOL.md). Do not invent a parallel tool set. The Phase 1 surface is the environment the forces already use.

### Discovery

`company.describe`, `company.bootstrap`, `constitution.get`, `constitution.amend`, `protocol.describe`

`protocol.describe` returns `natural_forces` (physics, including `handoff.write_ledger_then_next_force_reads`), `effectors` (optional particles, including sit-the-seat alignment), and the host-neutral `bet_workflow`. A fresh host uses the optional `company.describe` `cell_records` query to page authorized work, offers, frozen experiments, evidence and candidate learning. Experiment records include linked independent evaluation. This reuses existing discovery authority; it does not replace immutable live definitions or grant external-action approval. After a mutate that advances cell, run, evidence, lease, or tries, the next step is stop or this read-back — not a peer DM. That force-peer silence is distinct from external/host chat, which remains normal: acknowledge and reply when the founder or another external entity is waiting. See [`BET_OPERATING_GUIDE.md`](BET_OPERATING_GUIDE.md) for activation and resume flow.

### Agents and reflex work

`agent.register` (never grants a role), `agent.get`, `agent.list`
`work.create`, `work.claim`, `work.heartbeat`, `work.complete`, `work.fail`, `work.release`, `work.cancel`

An external runtime polls or is invoked for eligible work, then connects the agent to MCP. The substrate coordinates leases. `ai-nature-tick` is the Maintain/Continue clock, not a reflex scheduler.

### Offers, cells, experiments

`cell.create`, `cell.get`, `cell.list`, `cell.transition`, `cell.amend`
`offer.register`, `offer.mutate`
`experiment.register`, `experiment.start`, `experiment.close`, `experiment.amend`

Owner-signed `cell.amend` may revise the cell's objective text with a recorded
`founder_decision_ref`. It is own-cell and active-only, and it cannot change
budget, stage, owner, authority, or constitution.

`experiment.close` may record optional `reason` and `founder_decision_ref`.
They persist on the experiment and audit trail. Existing callers that omit
them keep working.

Owner-signed `experiment.amend` may align hypothesis window text with the
audited `window_ends_at`. Defend `evaluation.submit` must cite that window;
it does not parse dates out of hypothesis prose. A count criterion observed
0 with no qualifying fitness evidence may fail with empty `evidence_ids`;
passed still requires evidence.

`paused` → `active` and all `scaled` transitions fail closed until their gate, remediation, approval, and budget checks can be bound atomically. That is a kernel fact, not a reason to add another agent.

### Pheromones, evaluation, selection

`evidence.append`, `economics.append`
`evaluation.submit`, `gate.evaluate`
`allocation.propose`, `allocation.commit` (kill requires founder)

### Learning and audit

`learning.propose` persists a candidate from cited experiment, evaluation, and evidence. `learning.promote` remains fail-closed until the full promote gate exists; the proposer cannot promote.
`audit.list`, `decision.explain` — recorded inputs and rules, never hidden reasoning.

### Mutation contract

Every write requires `idempotency_key`, `expected_revision`, `correlation_id`, `operation_intent`, and a typed `payload`. Identity is never taken from those fields. Replay with the same principal, operation, key, and digest returns the original result. A stale revision fails without partial writes.

## 9. State machines and data (kernel facts)

Normative transitions live in [`STATE_MACHINES.md`](STATE_MACHINES.md). Builders must not invent extra states for force-personas.

```text
cell:        DRAFT → READY → ACTIVE ↔ PAUSED → SCALED
                               └──────────→ KILLED
experiment:  DRAFT → REGISTERED → RUNNING → EVALUATING → PASSED | FAILED | INCONCLUSIVE
                                      └──────────────→ CANCELLED
evidence:    ACTIVE → CORRECTED | SUPERSEDED | RETRACTED
learning:    CANDIDATE → TESTING → VALIDATED → PROMOTED | REJECTED | ROLLED_BACK
             (candidate persistence implemented; later lifecycle and promotion gated)
```

The executor cannot be the sole evaluator. Failed experiments remain inspectable. A new attempt after `KILLED` is a clone or child cell, not a resurrection.

Canonical operational tables remain company-scoped PostgreSQL projections plus append-only events: principals, definitions, bindings, runs, work items, leases, cells, offer versions, experiments, evidence, economics, evaluations, budgets (one unit of account per ledger), allocations, persisted learning candidates, skills, domain events, audit events, idempotency records. Candidate persistence through `learning.propose` is implemented; later lifecycle transitions and `learning.promote` remain gated. Git holds constitutions, schemas, fitness versions, migrations, and promoted knowledge. Git is not the runtime event queue.

## 10. Fitness, ledgers, and founder authority

Do not give every effector the scalar reward "make money."

**Stage 1 — eligibility.** All seven `fitness-v1` gates before any exploitation rank: legality/policy, truthfulness, customer-outcome floor, security/privacy, budget/cash, delivery-quality floor, data sufficiency and evaluator independence.

**Stage 2A — exploitation.** Expected 12-month contribution profit × confidence × repeatability ÷ capital at risk ÷ time to cash. Constitution-versioned integers; no silent optimizer.

**Stage 2B — exploration.** Separate ledger. Rank by value of information, not by mixing learning credit into profit.

Exploration and exploitation never share a ranking list or budget ledger. Containment (`pause`, `kill`) is not-ranked. Owner-led pause is kernel containment of a bound cell. Weekly Select is the allocator.

Budgets already accept exactly one unit of account per ledger: `money`, `time` (seconds), `cycles`, or `tries`. Offer price stays commercial money. Fitness score inputs (`P`, `K`, `V`, `E`) stay integer score quantities; they do not become time or tries when the operating ledger uses those units.

Sense-class work does not consume try `spent_minor`. Act complete/fail does.
Owner-signed `experiment.record_send` records one founder-signed external send
and consumes exactly one try. `sent_at` is the founder/Gmail-verified send time
and is not matched to evidence timestamps. Incremental try spend may set
`actual_amount` on an active reservation. The experiment `max_external_messages`
override is enforced at that debit when present; otherwise the stored
constitution `max_external_messages_per_experiment` is used. Remaining
sends are surfaced, and a send at the cap fails closed with the remaining count.
Owner-signed `experiment.amend` may set that override or change
`budget_amount_minor` through the reservation ledger with `founder_decision_ref`.
Owners read sends back through `company.describe` `cell_records` kind
`experiment_send` or cell-scoped `audit.list`.

Founder-only: constitution mutation, irreversible kill, major capital, high-risk market entry, strategy-level promotion. High-risk external actions stay founder- or human-gated: material spend or contract, pricing below floor, unsupported public claims, production deletion, security-boundary changes, regulated-market entry, irreversible customer communication at scale.

Secrets live in the deployment secret manager. Logs store references, not credentials.

## 11. Control, operational, and integration planes

**Git** holds low-frequency authority: constitution templates, protocol and schemas, fitness versions, ADRs, promoted skills and tests, migrations, release manifests.

**PostgreSQL** is canonical for operational state, work, evidence, reservations, evaluations, allocations, and audit. Runtime roles fail closed and must not bypass RLS.

**External adapters** remain the source of payment and product-usage facts. The durable adapter protocol is: record intent → verify policy and budget → reserve → invoke with a stable request id → record the observed result → compensate or release. Phase 1 uses fakes. Live adapters are follow-on product work after the forces are executable, not a substitute for them.

## 12. Hosted identity (kernel fact, not the next phase)

Hosted Streamable HTTP authenticates through Supabase OAuth/JWT. The substrate resolves the exact `(issuer, subject, client_id)` tuple plus the request's full resource URL to one active principal, instance, and run. Token or payload claims cannot select a role, company, cell, or agent.

One process advertises `ANC_PUBLIC_MCP_URL` (typically `https://<host>/mcp`) and may serve conservative path aliases (`https://<host>/mcp/<slug>`) so several connectors can present distinct OAuth resource URLs without extra Railway services. Identity exact-matches the full resource URL to the client's primary `resource_url` or an active `anc_oauth_client_resource_aliases` row. Path is not a host.

Every authenticated tool call appends a content-minimized `received` event and one `succeeded` / `denied` / `failed` terminal event with trusted attribution and digests. That identity boundary is implemented. The remaining hosted gates (dedicated deploy, one real authorization canary) do not complete the force topology and are not a reason to add more identity machinery.

Local stdio identity remains a test stub (`AI_NATURE_PRINCIPAL_ID` and related env) and must not be exposed as hosted authentication.

## 13. Technology baseline

- Python 3.12, `uv`, locked `pyproject.toml`.
- Official MCP Python SDK, pinned.
- Pydantic v2 and exported JSON Schema.
- stdio locally; authenticated Streamable HTTP when hosted.
- PostgreSQL (Supabase acceptable if boundaries stay explicit).
- Verified OAuth/JWT plus server-owned grants and RLS.
- Additive SQL migrations. Hosted `/ready` (Railway `healthcheckPath`) fails
  closed until every shipped `supabase/migrations` slug is applied; see
  [`DEPLOY_CONTRACT.md`](DEPLOY_CONTRACT.md). Versions are not compared.
- pytest contract, state-machine, security, replay, and persistence tests.
- Structured content-minimized logs.
- External, pluggable agent runtime. Do not select an agent framework until ordinary host sessions, MCP tools, durable state, and work claims are insufficient.

## 14. Delivery phases

Numbered Phase 1 in PROTOCOL remains the frozen kernel contract. Later "Phase 2–5" labels in older drafts mixed identity theater with commercial topology. Use the following reading instead.

### Kernel — implemented, do not re-litigate

Honest facts already in code and frozen contracts:

- server-resolved identity, definitions, instances, runs, and per-call attribution;
- work items and expiring leases;
- budgets in time, money, cycles, or tries; hierarchical company / cell / exploration / exploitation ledgers;
- Sense work does not debit try spend; Act complete/fail does;
- path MCP resource URLs on one hosted process;
- offer, experiment, evidence, evaluation, and allocation tools;
- owner-led allocate/contain for a bound cell;
- process-owned `ai-nature-tick` for lease expiry and stop-rule closeout (Maintain / Continue);
- evaluator ≠ executor (Defend);
- eligibility before rank (Select);
- no model in the server.

Phase 1 local proof (separate owner, specialist, evaluator principals through MCP, reconstructable ledger, fail-closed invalid transitions) is executable. Hosted identity hardening is implemented; a live canary may still be pending. **This is not a live commercial offer cell.**

### Forces — executable

Acceptance is behavioral, not theatrical, and is covered by contract tests:

1. `protocol.describe` advertises the eight forces as physics, not a persona list.
2. Sense (`sense_market`) complete/fail does not increment try `spent_minor`. Act complete/fail does. Owner-signed `experiment.record_send` spends exactly one try per recorded external send.
3. Transform keeps activity distinct from fitness; Select reads fitness.
4. Reflex work is leased: Sense and Act claim the next step from MCP; tick does not send the next message. A ledger write is the handoff; do not DM the next force. External/host chat remains normal and is not that force-peer silence.
5. Adaptation is `offer.mutate` inside the claim boundary, with constitution floors enforced.
6. Select is allocator + `fitness-v1`. Owner-led contain remains a documented temporary single-cell path.
7. Evolution stays founder-gated. Memory cannot self-promote.
8. Evidence appends carry the pheromone envelope; decay and hierarchy remain review, not a second fitness function.
9. Immune remains policy. No immune / keeper / strategy / force-bot definition was added.
10. Live sit-the-seat owner / specialist / evaluator bindings were not wiped; they submit to forces.

Do not start the next increment by adding more OAuth aliases, path MCP variants, identity roles, or eight force-personas.

### After this increment

- one real offer cell with live (non-fake) adapters for cash and product usage;
- sit-the-seat operation of Sense / Act / Defend / Select against that cell;
- typed hard-stop records for the remaining negative evidence classes;
- decay applied as deterministic review ordering once a rule version names the approximation;
- the remaining `learning.promote` lifecycle and gates (replay, attribution, controlled trial and rollback), building on already-persisted `learning.propose` candidates;
- clone / pause / kill workflows driven by the allocator, with founder kill.

Exit is attributable customer, cost, outcome, and cash evidence with no unauthorized action — not message volume.

### Later, only if one cell repeatedly makes money

Isolation, rate and cost controls, recovery testing, tenant export, SLOs. Do not generalize into a platform before the protocol makes money.

## 15. First vertical-slice facts that remain true

The Phase 1 acceptance path in PROTOCOL still holds and must not be weakened: bootstrap, bind principals, register and mutate an offer, preregister an experiment, lease work, separate evaluation, eligibility before allocation, reconstruct the ledger, fail closed on stale revision / overspend / self-evaluation, no model in the server.

What that path does **not** prove: a live commercial offer cell, sit-the-seat Grok bots for every labeled definition, or a completed Phase 4 promote gate. Those remain later work.

## 16. Explicit non-goals

- Minting immune, keeper, strategy, or eight force-persona Grok bots.
- Rewarding activity: leads, bookings, tasks completed, messages sent, experiments declared successful, memory volume, number of active agents.
- Putting a model, model router, or provider credential in the MCP server.
- Treating owner-led allocate/contain as Select.
- Treating `ai-nature-tick` as reflex chat or an agent scheduler.
- Treating Grok chat, SendToAgent, or channel membership as the work queue or force handoff.
- Using evidence hierarchy or decay as a hidden fitness score.
- Debiting try spend for Sense observation.
- Automatic promotion of observations into memory, policy, or skills.
- A chatbot society that imitates executives and departments.
- A centralized planner that dictates every task.
- Autonomous legal, banking, or contracting authority.
- Unconstrained outbound messaging.
- Automatic constitution changes.
- A marketplace of third-party agents or a universal ontology.
- Sophisticated bandits or RL before trustworthy evidence exists.

## 17. What a builder should change next

The eight forces are executable physics. Later work, still behind contract tests:

- sit-the-seat operation of Sense / Act / Select / Continue against one real cell without minting immune, keeper, strategy, or force-bots;
- decay or hierarchy as review ordering that does not alter `fitness-v1`;
- the remaining Phase 4 promote artifacts (replay, transfer test, controlled trial, rollback);
- live adapters for cash and product usage.

Point at this document and the signed README. Do not re-derive an eight-persona cast. Do not expand identity surface unless a real auth defect blocks a force path.

## 18. Technical thesis

The company is a constitutional market of end-to-end offer cells. Eight natural forces operate through the MCP physics engine at organism, colony, and civilization scales. Temporary effectors do leased work. Four nested routines are cadences of those forces. Immune is policy. The founder holds the genome and the kill switch. The kernel supplies identity, leases, Sense/Act spend law, evidence envelopes, owner-led contain, and a Maintain/Continue clock.

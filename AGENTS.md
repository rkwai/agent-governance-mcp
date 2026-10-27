# AGENTS.md — AI Nature Company

## Mission

Build AI Nature Company: a deterministic MCP physics engine that lets AI hosts operate end-to-end offer cells through eight natural forces — Bound, Sense, Act, Transform, Maintain, Defend, Select, Continue — learn from verified market evidence, allocate resources toward profitable behavior, and preserve founder control over irreversible decisions.

Hermetic: as above, so below. The same forces operate similarly at organism (offer cell), colony (portfolio), and civilization (founder genome) scales. Hosts and agents are temporary particles inside the forces, not standing personas.

The system exists to create customer value and risk-adjusted contribution profit. It is not an agent-roleplay framework and not a chatbot society.

## Read first

1. `README.md`
2. `docs/FOUNDING_CONVERSATION.md`
3. `docs/TECHNICAL_APPROACH.md`
4. For operating a bet: `docs/BET_OPERATING_GUIDE.md`. Read live MCP state before claiming provisioned identities, commercial results, or deployment.

## Non-negotiable architecture boundary

- Generative reasoning remains in the user-selected AI host.
- The MCP server must not invoke a model, accept model-provider credentials, store hidden chain-of-thought, or silently fall back to an inference service.
- The substrate owns deterministic identity, authorization, policy, budgets, state transitions, idempotency, concurrency, evidence envelopes, audit, and rollback.
- Never trust caller-supplied actor, role, company, cell, budget, or entitlement fields as authority.
- External actions must be attributable, bounded, idempotent where possible, and recorded with their actual result.
- Revenue, payment, product-usage, and delivery facts should come from authoritative systems or explicitly labeled manual attestations—not unsupported agent claims.
- An execution agent cannot be the sole evaluator of its own work.
- No observation automatically changes strategy, policy, memory, or a promoted skill.
- Constitution changes and irreversible actions require explicit founder authority.
- Never store secrets, tokens, passwords, private keys, raw credentials, or hidden reasoning in Git or application records.

## Product rules

- The offer cell is the end-to-end economic unit.
- Each experiment must be preregistered with a hypothesis, budget, time window, success threshold, failure threshold, and stop rule. Owner-signed `experiment.amend` may extend a running window, revise stop-rule, treatment, or hypothesis window text, change try-budget amount through the reservation ledger, or set an experiment `max_external_messages` override with `founder_decision_ref`; it must not retrofit success or failure criteria or amend the constitution. Defend judges the clock against `window_ends_at`.
- "Own cell" means the caller's agent binding covers the cell, not the cell's `owner_principal_id` (which may be the founder).
- The scheduled runtime tick runs in report mode only: one maintenance report per run, zero state mutations. Any mutating scheduled action needs its own flag and founder approval.
- Owner-signed `cell.amend` may revise a cell_owner's own active cell objective text with `founder_decision_ref`. It must not change budget, stage, owner, authority, or constitution.
- Offer mutations create a new immutable version with parent lineage; they do not silently overwrite history.
- Eligibility and safety gates run before fitness ranking.
- Exploitation capital and exploration capital are accounted for separately.
- Failed experiments remain inspectable and may not be rewritten as successes.
- Agent activity, message volume, and task completion are not company-level success measures.
- The eight forces are substrate physics, not roles or org-chart jobs. Do not mint eight force-personas.
- Sense observation MUST NOT debit try spend. Act complete/fail MUST. Owner-signed `experiment.record_send` records one founder-signed external send and MUST debit exactly one try, idempotently. The stored `max_external_messages_per_experiment` is enforced at that debit and MUST name remaining sends when it refuses.
- Transform keeps activity distinct from fitness. Select reads fitness, not packets.
- Sit-the-seat owner effectuates Bound (ICP/targets). Sense is a second specialist effector path (`sense_market`, no try debit). Act specialist (hunter) effectuates try-debit outbound from Sense-ready packets; founder signs every external send. Owner does not impersonate Select. Evaluator effectuates Defend.
- After a mutate that advances cell, run, evidence, lease, or tries: stop or read back via `company.describe` / `cell_records`. Shared MCP state is the force handoff. Do not DM a peer or SendToAgent for routine Sense↔Act↔Bound hops. Channel membership is not a work queue.
- Force-peer silence is not silence toward everyone. Founder, humans, and non-force agents stay on normal chat; acknowledge, do the work, and reply when they are waiting.

## Engineering workflow

- Begin new behavior with contract tests.
- Keep schemas and state transitions explicit and versioned.
- Prefer append-only domain events plus deterministic projections.
- Reject stale revisions and duplicate mutations safely.
- Make side-effecting operations replay-safe or compensatable.
- Keep outputs bounded and logs content-minimized.
- Run relevant tests and `git diff --check` before committing.

## Git conventions

- Default branch: `main`.
- Commit identity: `rkwai <rkwai@users.noreply.github.com>`.
- Keep commits coherent and imperative.
- Do not force-push or rewrite shared history without explicit approval.
- Do not claim a phase is implemented until its executable acceptance criteria pass.

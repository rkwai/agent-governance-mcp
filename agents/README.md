# Optional effector definitions

These JSON files are model-neutral runtime configuration validated by [`../schemas/agent-definition-v1.json`](../schemas/agent-definition-v1.json). They name optional effectors that submit to the eight natural forces in [`../README.md`](../README.md). They are not the eight forces, not standing personas, and not an org chart. They do not contain executable code, provider selection, or credentials. Registration never grants a role.

The forces are Bound, Sense, Act, Transform, Maintain, Defend, Select, and Continue. The MCP is the physics engine. These files are particles.

| Effector | Definition | Compatible role | Force | Reward | Do not reward |
|---|---|---|---|---|---|
| Sense specialist | `specialist` | `specialist` | Sense (`sense_market`) | List / enrich / door / buyer-identity quality | Lead or list volume |
| Market scout | `market-scout` | `specialist` | Act (`scout_market`) from Sense-ready packets | Try effectiveness after Sense-ready | Packets sent or list volume |
| Offer designer | `cell-owner` | `cell_owner` | Bound (does not impersonate Select) | Improvement over a registered baseline | Idea volume |
| Sales | `sales` | `specialist` | Act (`sales_step`) | Collected contribution profit adjusted for outcomes and retention | Bookings alone |
| Delivery | `delivery` | `specialist` | Act (`delivery_step`) | Verified outcome per delivery cost plus reusable assets | Tasks completed |
| Experiment evaluator | `evaluator` | `evaluator` | Defend | Calibration and cheap falsification | Declaring experiments successful |
| Memory | `memory` | `specialist` | Continue (`learning.propose`; cannot `learning.promote`) | Future performance uplift from reused knowledge | Volume of notes |
| Allocator | `allocator` | `allocator` | Select with `fitness-v1` | Portfolio profit, survival, and learning efficiency | Number of active agents |

Immune function is Bound + Defend policy in the substrate, not a standing chat agent. `immune/` remains leftover Phase 1 configuration. Do not mint an immune, keeper, strategy, or force-bot from it.

`specialist/` remains the generic leased-work definition used by live sit-the-seat specialist bindings. Sense is a second specialist effector path (`sense_market`), not a new force or a fourth live principal. Scout, sales, and delivery are additive labeled Act specialist definitions. Do not wipe or replace the live owner, specialist, or evaluator identity.

Sit-the-seat alignment:

- Owner effectuates Bound (ICP, targets, offer lineage). Owner-led contain does not impersonate Select. Bound learns ICP / target / offer adaptation.
- Sense specialist effectuates Sense (`sense_market`): lists, enrich, packets within Bound's frame. No try debit. Learns list / enrich / door / buyer-identity quality.
- Act specialist (hunter) effectuates Act (`scout_market`, sales, delivery) from Sense-ready packets only. Founder signs every external send. Complete/fail spends tries. Learns try effectiveness after Sense-ready — not list volume.
- Evaluator effectuates Defend. Producer cannot clear its own claims.

Routine Sense↔Act↔Bound handoffs are MCP writes plus reads. After a mutate that
advances cell, run, evidence, lease, or tries, the next step is stop or
`company.describe` / `cell_records` — not SendToAgent or a peer DM. Channel
membership is not a work queue. Force-peer silence is not silence toward
founder, humans, or other non-force agents; acknowledge and reply when they
are waiting.

For activation, resumption and independent evidence retrieval, use
[`../docs/BET_OPERATING_GUIDE.md`](../docs/BET_OPERATING_GUIDE.md) and the live
`protocol.describe` `natural_forces` and `bet_workflow` contracts. Existing definitions already declare
`company.describe`, whose `cell_records` query provides bounded, scoped work and
experiment context. This does not amend their capability declarations or grant
permission to execute external actions.

## Routines the kernel already executes

| Routine | Forces | Kernel behavior |
|---|---|---|
| Reflex | Sense, Act | `work.create` / `work.claim` for `sense_market`, `scout_market`, `sales_step`, and `delivery_step`. Those kinds cannot include evaluation or allocation capabilities. `sense_market` does not debit tries. Act kinds do. `ai-nature-tick` is not reflex. |
| Adaptation | Bound, Transform | Cell-owner `offer.mutate` creates an immutable child inside the bound cell. Constitution `pricing_floor_minor` is enforced. |
| Selection | Select, Defend | Allocator `gate.evaluate` → `allocation.propose` → `allocation.commit` with eligibility before rank and separate exploration/exploitation ledgers. Select reads fitness, not packets. Tick/cron closes due experiments and creates evaluator closeout work. Owner-led allocate/contain remains a temporary single-cell path when no allocator principal is bound. |
| Evolution | Continue, Bound | Founder-only constitution change, irreversible kill, major capital, and high-risk entry. Memory may propose a candidate; it cannot promote itself. |

An external runtime loads a definition, creates or invokes the agent, and authenticates that agent as a separate principal. Effective authority is the intersection of the definition declaration, server-owned role grants, resource scope, current policy, and any active work lease.

Do not add credentials, provider selection, executable hooks, hidden reasoning, or mutable company state to these files. A material instruction or capability change creates a new version with explicit supersession lineage; it does not edit the definition attributed to completed runs.

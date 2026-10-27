# Fitness and Allocation v1

Status: normative deterministic rule `fitness-v1`

Fitness means risk-adjusted contribution profit created through verified customer value. It is a portfolio decision, not a scalar reward handed to every agent.

## Decision order

Allocation is always two stage:

1. Apply every eligibility gate.
2. Rank eligible candidates within either the exploitation or exploration pool.

Containment actions (`pause` and `kill`) do not require a positive rank. `pause`
requires a server-verified, recorded `founder_authorized` or
`cell_owner_authorized` basis. `kill` still requires `founder_authorized`. A
verified policy violation or frozen hard stop takes the separate immediate-stop
path and can pause side effects without waiting for a portfolio decision. Caller
assertions that a policy or stop rule fired are never a containment basis.

## Stage 1: eligibility gates

The active constitution supplies thresholds. `fitness-v1` requires exactly these seven gate results:

| Gate | Pass condition |
|---|---|
| `legality_and_policy` | No open legal/policy block applies to the target, offer, population, channel, or action. |
| `truthfulness_and_claim_support` | Every material promise used by the experiment cites adequate current proof; no unresolved unsupported-claim event exists. |
| `customer_outcome_floor` | Verified outcome basis points meet the constitution minimum and no mandatory harm threshold fired. |
| `security_and_privacy` | No unresolved security/privacy incident or unauthorized data scope applies. |
| `budget_and_cash` | Spend and requested allocation fit company, cell, experiment, stage, and single-action limits in the constitution's unit of account. |
| `delivery_quality_floor` | On-time/accepted delivery basis points meet the constitution minimum and delivery overruns remain below stop rules. |
| `data_sufficiency_and_independence` | Minimum evidence count is met, sources and lineage are valid, and evaluation independence is verified. |

For exploitation, all seven MUST pass. A missing gate is a failure, not `unknown = pass`. Exploration MAY proceed with uncertainty in outcome or delivery only when the constitution explicitly permits the bounded experiment, but legality, truthfulness, security/privacy, budget/cash, and evaluator-independence requirements never relax.

Containment still records all seven gate results truthfully. Its eligibility is
derived only from the verified containment basis, not by changing failed or
unsupported evidence gates to `passed`. It has no funding amount or stage-ledger
reservation, and its deterministic rank is `not-ranked` with the exact fraction
`0/1`.

Refunds, complaints, churn, delivery overruns, manual exceptions, security incidents, and policy violations are first-class evidence. A configured hard-stop event overrides an otherwise positive score.

## Stage 2A: exploitation rank

For each eligible candidate, define integer inputs:

- `P`: expected 12-month contribution profit in minor currency units (a fitness score input, not the operating ledger unit);
- `C`: evidence confidence in basis points, `0..10000`;
- `R`: expected repeatability in basis points, `0..10000`;
- `K`: capital at risk in minor units;
- `T`: expected time to collected cash in whole days.

The stored rational score is:

```text
numerator   = P * C * R * 30
denominator = max(K, 1) * max(T, 30) * 10000 * 10000
```

No floating-point value is authoritative. Compare candidates by cross multiplication of numerator and denominator using unbounded integers. Negative expected profit produces a negative score and is not eligible for exploitation funding under the default constitution.

Contribution profit is collected or probability-adjusted revenue minus direct delivery cost, channel cost, model/tool cost, refunds, support burden, and other incremental operating cost. Bookings alone are not revenue; gross revenue alone is not contribution profit.

Tie-break order is deterministic:

1. higher rational score;
2. lower capital at risk;
3. shorter time to cash;
4. earlier proposal server timestamp;
5. lexicographically smaller target UUID.

## Stage 2B: exploration rank

For each constitution-eligible uncertain hypothesis, define:

- `V`: expected value of the decision information in minor units;
- `Q`: probability the test resolves the named uncertainty in basis points, `0..10000`;
- `E`: maximum experiment cost in minor units;
- `D`: maximum duration in whole days.

The stored rational score is:

```text
numerator   = V * Q
denominator = max(E, 1) * max(D, 1) * 10000
```

Tie-break order is higher score, lower cost, shorter duration, earlier proposal timestamp, then target UUID. Exploration and exploitation candidates never share a ranking list or budget ledger.

`V` must identify the concrete future decision, plausible outcome values, and current uncertainty. An agent cannot raise rank merely by asserting that an experiment is interesting.

## Confidence and evidence routing

Evidence attention may use a decayed routing signal:

```text
strength
* confidence_bps / 10000
* provenance_weight_bps / 10000
* causal_weight_bps / 10000
* exp(-age_seconds / half_life_seconds)
```

This routing signal prioritizes review; it is not the allocation score. Because exponential decay is not an exact integer operation, Phase 1 stores the raw inputs and the server-computed observation time. It MUST NOT use the decayed value as an eligibility or capital decision unless a later rule version specifies a deterministic approximation.

Default evidence ordering is:

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

The hierarchy guides provenance review; it never converts a weak proxy into cash evidence.

`fitness-v1` ranking inputs (`P`, `K`, `V`, `E`, and related `*_minor` score
fields) remain integer score quantities. They do not become time, cycles, or
tries when the operating ledger uses those units. `budget_and_cash` compares
spend and requested allocation in the constitution unit; exploitation rank still
scores expected contribution profit. Offer `pricing_floor_minor` remains money.

## Allocation commit

A proposal conforming to `allocation-v1.json` records:

- action, target ID, and exact target revision;
- exploration, exploitation, or containment stage;
- every gate result and its evidence IDs;
- formula ID, raw input snapshot, and exact score fraction;
- positive amount in the constitution unit of account for `fund`; exactly null for `maintain`, `clone`,
  `mutate`, `pause`, and `kill`;
- constitution ID/version and `fitness-v1`;
- SHA-256 digest of the canonical decision input;
- proposer principal/run and concise rationale.

For containment, the companion decision projection and proposal/commit events
record the server-stamped basis (kind, summary, authorizing principal, and server
time), while the allocation input digest binds that basis to the proposal. The
founder must have a founder-only approval grant that applies at the current company
root and must be named by the active constitution. A cell-owner pause basis must be
stamped for the bound cell owner. A stale gate constitution cannot be rebound to a
new proposal.

Commit re-evaluates identity, applicable grant, trusted cell binding, target
revision, gates, active constitution, stop state, and—only for a positive funding
amount—available stage budget in one transaction. `pause` is a bounded
cell-owner or deferred-allocator commit; `kill` requires founder-only irreversible
authority. A containment commit
atomically records the allocation and decision and transitions the exact target
cell revision, without a budget reservation. The committed record always adds the
deciding principal and server time. Its run is also required except for a
server-verified human founder committing `kill`, where the run remains null rather
than being fabricated. A successful replay never reserves funds or transitions the
cell twice.

## Agent-local measures

These measures shape evaluation; they do not create authority:

- Scout: signals that later become validated demand, adjusted for false-positive rate.
- Sales: collected contribution profit adjusted for outcomes, refunds, and retention.
- Delivery: verified customer outcome per direct delivery cost plus transferred reusable capability.
- Evaluator: calibration and useful falsification, not pass rate.
- Allocator: portfolio economics, survival, and decision value per exploration dollar.
- Immune: harmful action prevented with low false-positive containment cost.

No role is rewarded for lead volume, bookings, completed tasks, pass rate, or active-agent count in isolation.

## Versioning

Changing a gate, threshold interpretation, formula, input definition, tie-breaker, or evidence treatment creates a new rule version and a new constitution version. Historical decisions retain their original inputs and rules. Backtesting a proposed rule does not rewrite those decisions.

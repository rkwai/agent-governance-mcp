# Org design for AI-native companies

This is the argument behind the code. The README has the short version and the feature map.

## 1. What the middle layers were for
In a traditional company, a lot of people do relay work. They take what customers said, turn it into notes, compare it to plans, turn it into tickets, assign it, check it, and report status back up. Managers stack because each one can only coordinate so many people.

I call that the legion shape: a front line, then rank after rank of people passing context up and orders down. It works, but the relay is where most of the time goes, and every hop loses something.

## 2. What changes when agents carry context
Agents can now do most of that relay: keep context current, compare it with work in motion, run bounded loops, check their own output and generate status. So the coordination layers stop being required.

The work that's left for people is the work at the two ends:
- **Interfacing with reality:** customers, markets, trust, noticing what changed.
- **Designing the system:** which agents exist, what they may do, how their work gets checked, and the decisions that can't be undone.

## 3. The possible structures
These are the shapes this substrate is built to support. They're options, not one blueprint.

**Small core, many cells.** A few humans own direction and irreversible calls. Each business bet is a *cell*: an owner agent, specialists, an evaluator and a budget. The idea is that cells producing evidence get more budget, and cells that don't get paused or killed. Pause and kill are enforced today. Evidence-based allocation exists but is early.

**One cell per client.** For service work, each client gets a cell with its own owner agent. That makes it clear who owns that client's asks, and it limits what can go wrong to one client.

**Shared immune function.** One role watches across cells. It can pause a cell and read the audit trail, but it can't do the cell's work. It's a check, not a manager.

**An allocator, not a budget meeting.** Budget moves through proposals and bounded commits into ledgers. Big or irreversible commits go to the human core.

## 4. What has to be true for any of this to be safe
An org chart made of agents is only as good as its rules, and a rule that's just written down isn't enough. It has to be enforced where the action happens:

1. **Identity decides authority.** An agent can't talk its way into a permission.
2. **Roles are narrow.** Each role has a fixed capability set, and everything else is denied.
3. **Execution and evaluation are separate.** No agent grades its own work.
4. **Spend is bounded.** Work spends against a reservation, and it stops when the reservation runs out.
5. **Everything can be stopped.** Killing a cell ends its live runs. Runs expire on their own.
6. **Irreversible means human.** The founder role holds those capabilities, and outside sends carry a human sign-off reference.
7. **Everything is attributable.** Every call is recorded with who made it, including the ones that were denied.

Each of these maps to code. See the table in the [README](../README.md#org-idea--what-the-code-enforces).

## 5. What's not solved here
- Choosing good bets still takes human judgment. Fitness scoring exists, but it's early.
- The agents themselves (their prompts, models and hosts) live outside this repo.
- Trust and accountability still sit with people. The substrate makes the agents' behavior visible and bounded. It doesn't make anyone less responsible for the result.

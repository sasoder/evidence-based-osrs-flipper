# Planner evaluation contract

This directory is the frozen oracle for planner-improvement work. Planner code may change;
the contract, runner, fixtures, and evaluator tests must not change inside an optimization loop.
Any evaluator change is a separate, human-reviewed change that regenerates every baseline.

## Purpose

The evaluator answers a narrower question than the planner's unit tests: given the same information
at decision time, does a plan remain safe, capital-efficient, and profitable on unseen future
buckets across bankrolls, attendance horizons, slot counts, item price tiers, and market regimes?

The fixtures contain two disjoint sections. `history` and the current quote are visible to the
planner. `future` is withheld until the plan has been produced and is used only by the execution
simulator. The runner patches the Wiki price client with fixture data, never the signal or planner
outputs.

## Hard invariants

- A plan cannot exceed available cash, GE slots, or the requested slot cap.
- Active offers cannot be recommended when attendance makes their 30-minute management deadline
  impossible.
- Every order must have positive expected profit and clear the reservation return in
  `contract.json`, measured against **posted capital** and the lane's full capital horizon.
- Simulated loss per position and per portfolio must remain within the configured bankroll limits.
- For identical market, attendance, and slots, increasing cash must not reduce either planner
  expected profit or withheld-future simulated profit. More cash expands the feasible set; the
  planner may retain the extra cash.

A baseline is allowed to violate invariants. An optimization candidate is not.

## Execution simulation

The simulator is deliberately conservative and deterministic. A resting entry fills only in a
future bucket whose low-side transaction price touches the bid. Fill quantity is capped by a fixed
participation rate applied to low-side volume. A target sale similarly needs a high-side touch and
sell-side capacity. Remaining inventory is force-sold at the final low-side transaction price and
taxed. Unfilled posted capital accrues reservation cost until cancellation; filled capital accrues
it through liquidation.

Wiki buckets cannot reveal queue position. These results are controlled comparisons, not claims of
precise live execution. FU fills should eventually calibrate the fixed participation assumptions.

## Corpus policy

- `core_market.json` is permanent regression evidence and is never randomly dropped.
- Observed fixtures retain every eligible item by default, so the planner—not fixture construction—
  decides which items are candidates at each bankroll and point in time.
- Future rolling snapshots may use stratified reservoir sampling with exponential age weights.
- Every randomized cohort records its seed.
- A sealed temporal corpus must not be inspected during iterative optimization.
- New fixtures never replace known crash, stale-data, bad-tick, or capital-efficiency regressions.

## Acceptance loop

One iteration changes planner code only, runs unit tests and this evaluator, and records its result.
During the initial repair phase, a checkpoint must strictly reduce the total violation count, add
no new violation class, improve aggregate utility by the declared margin, and not worsen the worst
scenario. Once violations reach zero, strict mode requires them to stay at zero. Each accepted
iteration is one commit. Stop after five consecutive rejected iterations or another declared
budget; do not tune the evaluator to admit a candidate.

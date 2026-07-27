# Evaluator v2 phase-one characterization

This report records infrastructure and characterization only. Evaluator v2 is not accepted,
frozen, or ready to gate planner-improvement work. The accepted v1 baseline was not regenerated
or replaced.

## Implemented contract

- Results persist the executable order fields `item_id`, `side`, `quantity`, `buy_price`,
  `sell_target`, `cancel_after`, and `hard_exit_after`. Signatures ignore planner-controlled
  labels and explanations.
- Planner inputs are a whitelist projection of current quotes, item metadata, and history.
  Future buckets and any coverage-named metadata are rejected. Visible challenger construction
  has no outcome or coverage parameters.
- Coverage is fixed per `(fixture, item, lane, as_of)`. Covered scoring cells remain independent;
  an uncovered changed action receives no performance credit.
- Visible evidence uses non-overlapping entry-plus-hold blocks. Entry-opportunity episodes are
  counted separately using a full hard-exit horizon between episode starts.
- The canonical frontier is independent of planner output: for each fixture and lane, it ranks
  one current-quote action per visible item by visible replay utility and retains eight. This
  bounds evaluation without scanning raw buy/sell price pairs. It is not a globally optimal
  frontier.
- Quantity candidates include integer neighbors around planner quantities, replay fill capacity,
  target and forced-exit capacity, reservation rounding, profit-zero and risk crossings, GE
  limits, and affordability. Utility uses exact rational accumulation and is rounded once at the
  portfolio level.
- One-order additions, next-breakpoint resizes, one-for-one replacements, adjacent-bankroll
  scaling, and the bounded benchmark are report-only. Their metrics are not acceptance failures.

The frontier intentionally uses one current-quote price pair per item/lane. Alternative price
policy, uncertainty qualification, and materiality thresholds remain human decisions.

## Verification

- `uv run python -m unittest`: 161 tests passed, with one intentional expected failure for the
  unresolved tax-exemption discrepancy.
- Complete reviewed-base evaluator: 432 cases in 82.251 seconds.
- `git diff --check`: passed.
- Planner files and `evaluation/baselines/main.json`: unchanged.

The reviewed-base run still reports the v1 baseline's existing 133 case violations and 128
dominance violations. This phase does not reinterpret or accept them.

## `72feb46` adversarial characterization

The run used a disposable detached worktree at
`72feb46efe572437c2cb2bf8bef5fb56cd1b1fa7`, with only evaluator and evaluator-test files overlaid
from this branch. No experiment code was merged or cherry-picked.

- Complete runtime: 81.177 seconds for 432 cases.
- Total report-only challengers: 10,576.
- All-window-dominant challengers: 158.
- Raw positive-mean challengers: 3,448.
- Structurally evidence-qualified challengers: 8,849.
- Unchanged adjacent-bankroll transitions with a positive-mean, evidence-qualified challenger:
  276 of 360.
- Completely flat six-bankroll groups with such a challenger: 42.

Each table cell is `challengers / all-window / positive-mean / evidence-qualified`.

| Fixture | Patient | Patient probe | Active | Time of day |
| --- | ---: | ---: | ---: | ---: |
| `stable_seed_11` | 354 / 0 / 158 / 318 | 250 / 0 / 142 / 228 | 108 / 0 / 108 / 108 | 102 / 0 / 54 / 80 |
| `stable_seed_29` | 464 / 0 / 195 / 452 | 102 / 0 / 80 / 80 | 108 / 0 / 84 / 24 | 102 / 0 / 54 / 80 |
| `quiet_seed_47` | 447 / 0 / 54 / 357 | 150 / 0 / 54 / 90 | 150 / 0 / 90 / 90 | 150 / 0 / 54 / 90 |
| `reversal_seed_71` | 198 / 0 / 0 / 162 | 198 / 0 / 0 / 162 | 198 / 0 / 0 / 198 | 198 / 0 / 0 / 162 |
| `crash_guard_seed_89` | 198 / 0 / 0 / 162 | 198 / 0 / 0 / 162 | 198 / 0 / 0 / 198 | 252 / 0 / 54 / 216 |
| `wiki_cache_2026-07-11` | 1,751 / 0 / 443 / 1,365 | 465 / 0 / 262 / 465 | 66 / 58 / 58 / 0 | 86 / 0 / 26 / 50 |
| `wiki_cache_2026-07-22` | 944 / 0 / 341 / 944 | 458 / 2 / 188 / 458 | 188 / 44 / 61 / 51 | 126 / 0 / 0 / 90 |
| `wiki_cache_2026-07-26` | 2,205 / 0 / 762 / 1,935 | 54 / 0 / 18 / 18 | 54 / 54 / 54 / 0 | 54 / 0 / 54 / 54 |

Frontier cardinalities are planner-invariant:

| Fixture | Actions |
| --- | ---: |
| `stable_seed_11` | 16 |
| `stable_seed_29` | 16 |
| `quiet_seed_47` | 16 |
| `reversal_seed_71` | 16 |
| `crash_guard_seed_89` | 16 |
| `wiki_cache_2026-07-11` | 27 |
| `wiki_cache_2026-07-22` | 32 |
| `wiki_cache_2026-07-26` | 29 |

## Tax findings

The 2% floor and 5,000,000-coin per-item cap in `flipper.ge_tax.sale_tax` agree with the current
documented rule. Evaluator v1 and planner signal calculations both call `sale_tax` without item
metadata, so they tax every sale. Realized RuneLite accounting calls `net_sale_price`, which tries
to honor exemptions by item id or name.

That means evaluator v1 does not match realized runtime accounting for tax-exempt items. The
runtime exemption metadata is also incomplete or stale against current mapping names. Examples
include teleport items whose mapping names now end in `(tablet)`, `Shrimps`, `Games necklace(8)`,
`Glassblowing pipe`, and `Civitas illa fortis teleport`. The Wiki real-time-price mapping provides
ids and names but no tax-exemption flag, so the evaluator cannot infer a reviewed exemption
contract from fixture metadata.

The v2 simulation deliberately retains v1 tax behavior. An expected-failing characterization test
demonstrates the Hammer discrepancy. No new tax rule is selected or frozen here.

References:

- https://oldschool.runescape.wiki/w/Grand_Exchange#Convenience_fee_and_item_sink
- https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices#Mapping

## Cross-lane evidence

Cross-lane benchmarking remains unresolved. The 5-minute, 1-hour, and 6-hour lane horizons do not
form aligned replay samples, so the evaluator reports every benchmark and challenger by lane. It
does not calculate cross-lane confidence by treating those vectors as interchangeable.

A sound resolution requires a common time-indexed portfolio replay with explicit cash reservation,
overlapping positions, and synchronized valuation/exit times.

## Uncertainty calibration

The effective independent sample count is not identifiable from the checked-in corpus. The maximum
non-overlapping pseudo-checkpoint counts before accounting for overlapping dated fixtures,
shared regimes, or synthetic-seed dependence are:

- Patient: 88.
- Patient probe: 88.
- Active: 40.
- Time of day: 348.

Even the time-of-day upper bound is not an established independent count. Under an iid assumption,
299 observations are needed merely for a 95% chance of seeing at least one event from a 1% tail;
that is not enough by itself to estimate a stable 99th percentile. A 99th-percentile uncertainty
threshold is therefore unsupported.

Additional data must be prospective and time-indexed, span at least 299 demonstrably independent
lane horizons, retain overlap and regime labels, and be collected under a protocol fixed before
planner tuning.

## Sealed holdout boundary

A directory outside the repository is not sealed when the planner agent has filesystem access.
A real holdout requires a separately permissioned service or execution principal that:

- never exposes raw outcomes to the planner principal;
- accepts a planner artifact or revision through a narrow submission interface;
- runs evaluation in an isolated account/environment;
- returns only pre-approved aggregate results; and
- keeps access and submission logs controlled outside the planner workspace.

No sealed service is built or simulated in this phase.

## Human decisions still required

Before challenger metrics can become acceptance gates, reviewers must choose:

- the canonical price policy and whether eight items per lane is materially sufficient;
- uncertainty and materiality thresholds, including multiple-comparison treatment;
- the minimum replay-block and distinct-episode evidence rule;
- a common time-indexed method for cross-lane portfolio comparison;
- the authoritative tax-exemption metadata and migration treatment;
- risk-limit crossing semantics for mixed portfolios; and
- the prospective data and sealed-holdout operating protocol.

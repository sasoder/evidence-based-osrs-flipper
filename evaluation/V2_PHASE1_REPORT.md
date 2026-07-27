# Evaluator v2 phase-one characterization

This report records infrastructure and characterization only. Evaluator v2 is report-only: it is
not accepted, frozen, or an acceptance gate for planner work. The accepted v1 baseline was not
regenerated or replaced.

## Corrected phase-one contract

- Evaluator-owned output sections determine lanes: `buys` is patient, `patient_probes` is
  patient-probe, `active_buys` is active-margin, and `time_buys` is time-of-day. Simulation consumes
  normalized executable orders. Planner-controlled `strategy`, `bucket`, `name`, `reason`, and
  constraint text cannot select simulation semantics.
- Planner inputs are a whitelist projection of current quotes, item metadata, and history. Future
  buckets and coverage-named metadata are rejected.
- The canonical frontier is independent of planner output. For each fixture and lane, the evaluator
  scans visible fixture items, scores one current-quote action per item, and retains eight. This is
  a bounded visible-data frontier, not a set of planner-emitted actions and not a global optimum.
- Every addition, resize, replacement, adjacent-bankroll scaling alternative, and bounded benchmark
  is checked as a complete portfolio for total cash, total slots, attendance, duplicate items, and
  GE limits. A lane benchmark holds other lanes fixed and receives only the remaining cash and
  slots.
- Quantity construction calculates fill, target, forced-exit, affordability, GE-limit,
  utility-zero, position-loss, and lane-local portfolio-loss points and their retained integer
  neighbors. Additions and replacements evaluate every feasible retained point, then deterministically
  choose maximum visible mean utility, breaking ties by lower cost and quantity.
- Visible evidence belongs only to changed post-change actions. The stored block and distinct
  opportunity-episode counts use the minimum across those actions, so an unchanged order cannot
  donate evidence. Continuous touches form one episode until a non-touch interval separates them.
- `all_window_dominance` means every comparable visible delta is nonnegative and at least one is
  strictly positive.
- Coverage is recorded per fixture, item, lane, and `as_of`. It requires every bucket field, integer
  timestamps strictly after `as_of`, strict ascending order, exact timestep spacing, and the full
  entry-plus-hold horizon. The sorted manifest has a deterministic SHA-256.
- Withheld scoring records coverage for each changed before/after action. A fully covered change
  receives a raw signed outcome delta; otherwise its status is `uncovered` and its delta is null.
  There is no positive-only performance credit or acceptance score.

Risk replay remains lane-local. `lane_local_position_risk_compliant` checks each alternative
position against the position-loss limit, while `lane_local_portfolio_risk_compliant` checks the
sum of that lane's replayed profits against the lane portfolio-loss limit. Every challenger
explicitly records `full_portfolio_risk_assessed: false`; neither field claims synchronized
cross-lane portfolio risk.

## Classification predicates

The report persists these independently:

- `raw_positive_mean`: the mean incremental visible utility is greater than zero.
- `provisional_3_block_2_episode`: every changed post-change action has at least three
  non-overlapping blocks and two distinct opportunity episodes.
- `lane_local_position_risk_compliant`.
- `lane_local_portfolio_risk_compliant`.
- `provisional_positive_mean_evidence_and_lane_risk`: the conjunction of all four predicates above.

The three-block/two-episode rule is provisional. The conjunction is a reproducible descriptive
count, not a statement that a challenger qualifies for acceptance.

## Verification and performance

| Run | Cases | Runtime | Output size | Planner matrix | V2 characterization |
| --- | ---: | ---: | ---: | ---: | ---: |
| Reviewed base `0adfe382` with corrected evaluator | 432 | 31.846 s | 21,580,078 bytes (20.58 MiB) | 26.493 s | 3.341 s |
| Detached `72feb46` with corrected evaluator overlaid | 432 | 29.278 s | 25,876,911 bytes (24.68 MiB) | 22.299 s | 4.540 s |

The complete runtime target of under 60 seconds is met. On the reviewed-base run the largest phase
is the 26.493-second planner matrix; input projection took 1.060 seconds, frontier construction
0.946 seconds, and result summarization 0.007 seconds.

`uv run python -m unittest` passes 169 tests with one intentional expected failure for the unresolved
tax-exemption discrepancy. `git diff --check` passes. Planner files and
`evaluation/baselines/main.json` remain unchanged.

The reviewed-base run still reports the v1 baseline's existing 133 case violations and 128
dominance violations. This phase does not reinterpret or accept them. The detached `72feb46` run
reports zero v1 case or dominance violations, which also does not make its v2 challenger metrics an
acceptance result.

## Corrected aggregate counts

| Predicate | Reviewed base | Detached `72feb46` |
| --- | ---: | ---: |
| Total report-only challengers | 7,184 | 8,751 |
| Strict all-window dominance | 318 | 54 |
| Raw positive mean | 3,353 | 4,723 |
| Provisional three-block/two-episode shape | 6,110 | 7,922 |
| Lane-local position-risk compliant | 6,707 | 8,559 |
| Lane-local portfolio-risk compliant | 7,138 | 8,715 |
| Positive mean + provisional evidence + both lane-local risk checks | 3,114 | 4,672 |

On the detached `72feb46` run:

- 276 of 360 unchanged adjacent-bankroll transitions have at least one challenger at the higher
  bankroll satisfying positive mean + provisional three-block/two-episode evidence + both
  lane-local risk checks.
- 42 of 72 completely flat six-bankroll groups have at least one challenger satisfying that same
  conjunction.

These two corrected values happen to equal the old headline integers. The prior “qualifying”
interpretation is withdrawn; only the exact descriptive predicates above were reproduced.

The detailed detached-run cells below are
`challengers / strict all-window / raw positive mean / provisional evidence / position risk /
lane portfolio risk / conjunction`:

| Fixture and lane | Counts |
| --- | ---: |
| `stable_seed_11:patient` | 352 / 0 / 182 / 294 / 352 / 352 / 182 |
| `stable_seed_11:patient-probe` | 184 / 20 / 98 / 162 / 184 / 184 / 98 |
| `stable_seed_11:active-margin` | 32 / 0 / 32 / 32 / 32 / 32 / 32 |
| `stable_seed_11:time-of-day` | 74 / 0 / 52 / 52 / 74 / 74 / 52 |
| `stable_seed_29:patient` | 464 / 0 / 179 / 324 / 464 / 464 / 179 |
| `stable_seed_29:active-margin` | 32 / 0 / 22 / 32 / 28 / 32 / 22 |
| `stable_seed_29:time-of-day` | 74 / 0 / 52 / 52 / 74 / 74 / 52 |
| `quiet_seed_47:patient` | 351 / 0 / 162 / 261 / 351 / 351 / 162 |
| `quiet_seed_47:patient-probe` | 132 / 0 / 72 / 72 / 132 / 132 / 72 |
| `quiet_seed_47:active-margin` | 44 / 0 / 24 / 24 / 44 / 44 / 24 |
| `quiet_seed_47:time-of-day` | 132 / 0 / 72 / 72 / 132 / 132 / 72 |
| `reversal_seed_71:patient` | 198 / 0 / 0 / 162 / 180 / 189 / 0 |
| `reversal_seed_71:active-margin` | 66 / 0 / 0 / 66 / 57 / 66 / 0 |
| `reversal_seed_71:time-of-day` | 198 / 0 / 0 / 162 / 180 / 189 / 0 |
| `crash_guard_seed_89:patient` | 198 / 0 / 0 / 108 / 180 / 189 / 0 |
| `crash_guard_seed_89:active-margin` | 66 / 0 / 0 / 48 / 57 / 66 / 0 |
| `crash_guard_seed_89:time-of-day` | 252 / 0 / 108 / 162 / 234 / 243 / 108 |
| `wiki_cache_2026-07-11:patient` | 1,751 / 0 / 855 / 1,751 / 1,751 / 1,751 / 855 |
| `wiki_cache_2026-07-11:patient-probe` | 417 / 2 / 272 / 417 / 417 / 417 / 272 |
| `wiki_cache_2026-07-11:time-of-day` | 36 / 0 / 36 / 36 / 36 / 36 / 36 |
| `wiki_cache_2026-07-22:patient` | 944 / 0 / 572 / 944 / 846 / 944 / 533 |
| `wiki_cache_2026-07-22:patient-probe` | 370 / 2 / 235 / 370 / 370 / 370 / 235 |
| `wiki_cache_2026-07-22:active-margin` | 53 / 12 / 18 / 24 / 53 / 53 / 6 |
| `wiki_cache_2026-07-22:time-of-day` | 126 / 0 / 0 / 90 / 126 / 126 / 0 |
| `wiki_cache_2026-07-26:patient` | 2,205 / 18 / 1,680 / 2,205 / 2,205 / 2,205 / 1,680 |

An absent fixture/lane row means that run constructed no challenger for that cell; it is not a
fixed cohort assertion.

Frontier cardinalities remain planner-independent for these runs:

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

## Coverage characterization

Both complete runs produced the same manifest hash:
`0bda4722c34e54639e8fa42374f3764fa1d758c7e360d5b7e4470febbbd6464e`.
Of 10,560 cells, 3,591 are fully covered. The uncovered reason combinations are:

| Reasons | Cells |
| --- | ---: |
| Incomplete horizon | 5,078 |
| Unexpected timestep spacing | 1,691 |
| Incomplete horizon and unexpected spacing | 120 |
| Timestamp not strictly after `as_of` | 80 |

In particular, a synthetic bucket exactly equal to `as_of` is now uncovered. Coverage is fixed for
this run's fixture/as-of/item/lane cells, but no broader permanent cohort is claimed.

## Tax findings

The 2% floor and 5,000,000-coin per-item cap in `flipper.ge_tax.sale_tax` agree with the documented
rule. Evaluator v1 and planner signal calculations both call `sale_tax` without item metadata, so
they tax every sale. Realized RuneLite accounting calls `net_sale_price`, which tries to honor
exemptions by item id or name.

Evaluator v1 therefore does not match realized runtime accounting for tax-exempt items. The
runtime exemption metadata is also incomplete or stale against mapping names. The Wiki
real-time-price mapping provides ids and names but no tax-exemption flag, so the evaluator cannot
infer a reviewed exemption contract from fixture metadata.

The v2 simulation deliberately retains v1 tax behavior. An expected-failing characterization test
demonstrates the Hammer discrepancy. No new tax rule is selected or frozen here.

References:

- https://oldschool.runescape.wiki/w/Grand_Exchange#Convenience_fee_and_item_sink
- https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices#Mapping

## Cross-lane evidence

Cross-lane risk and benchmarking remain unresolved. The 5-minute, 1-hour, and 6-hour lane horizons
do not form aligned replay samples, so the evaluator reports every risk result by lane. It does not
calculate full-portfolio risk or cross-lane confidence by treating those vectors as interchangeable.

A sound resolution requires a common time-indexed portfolio replay with explicit cash reservation,
overlapping positions, and synchronized valuation and exit times.

## Uncertainty calibration

The effective independent sample count is not identifiable from the checked-in corpus. The maximum
non-overlapping pseudo-checkpoint counts before accounting for overlapping dated fixtures, shared
regimes, or synthetic-seed dependence are:

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

A directory outside the repository is not sealed when the planner agent has filesystem access. A
real holdout requires a separately permissioned service or execution principal that:

- never exposes raw outcomes to the planner principal;
- accepts a planner artifact or revision through a narrow submission interface;
- runs evaluation in an isolated account or environment;
- returns only pre-approved aggregate results; and
- keeps access and submission logs controlled outside the planner workspace.

No sealed service is built or simulated in this phase.

## Human decisions still required

Before any challenger metric can become an acceptance gate, reviewers must choose:

- the canonical price policy and whether eight items per lane is materially sufficient;
- uncertainty and materiality thresholds, including multiple-comparison treatment;
- whether the provisional three-block/two-episode rule is acceptable;
- a common time-indexed method for full cross-lane portfolio risk and comparison;
- the authoritative tax-exemption metadata and migration treatment;
- risk-limit crossing semantics for synchronized mixed-lane portfolios; and
- the prospective data and sealed-holdout operating protocol.

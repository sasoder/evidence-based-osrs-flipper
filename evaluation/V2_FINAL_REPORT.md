# Evaluator V2 final freeze report

Evaluator V2 is the frozen standalone lane-local oracle. Human review approved the selected 0.5bp
challenger-materiality floor. Planner optimization has not begun, and no file under `flipper/`
changed.

## Frozen contract

The final evaluator:

- makes attendance part of executable identity and replay;
- prevents an overnight patient offer from being cancelled, replaced, or managed before the
  player's 12-hour return, with a 24-hour full horizon;
- selects lane universes without planner or signal code;
- constructs challengers from visible inputs only and attaches withheld outcomes afterward;
- evaluates every feasible generated quantity breakpoint and retains the complete exact-utility
  nondominated set through per-item and global Pareto reduction;
- scores submissions and challengers with the same utility, evidence, and risk functions;
- uses a bankroll-relative materiality floor instead of treating every positive fraction as a
  planner failure;
- labels all gates lane-local and makes no whole-planner portfolio-optimality claim;
- gates observed and synthetic fixtures separately; and
- has no utilization or per-case realized cash-dominance requirement.

The adversarial suite independently compares the returned frontier with exhaustive reduction over
all generated breakpoint candidates. It also covers attendance timing, pre-return management,
attendance-keyed coverage, timing spoofing, withheld-data isolation, structural output semantics,
partial inventory, same-bucket capacity, evidence episodes, archive integrity, and separate cohort
gates. `uv run python -m unittest` passes 165 tests.

Two clean `origin/main` runs were semantically identical after excluding only generation timestamps
and runtime measurements. Their complete runtimes were 89.890 and 89.656 seconds.
The final compact-baseline regeneration took 91.279 seconds and remained semantically identical to
the prior full result after also excluding evaluator revision and the oracle hash changed by the
new projection test.
`git diff --check` passes, and `git diff 34954d1 -- flipper` is empty.

## Attendance semantics

Patient attended and away-2h cases keep the 4-hour entry cancellation and 16-hour full horizon.
The overnight patient case cannot be managed until hour 12, cancels any remaining buy quantity at
hour 12, and force-exits remaining inventory at hour 24. A target sell for an early full fill is
not placed before the player returns.

A reported challenger replacement is a counterfactual order at the original cutoff. The evaluator
does not simulate an in-horizon cancel-and-reprice action.

## Material-improvement characterization

Counts below are cases with at least one eligible challenger meeting the named fraction of case
bankroll:

| Threshold | 10m floor | 100m floor | 1b floor | `origin/main` | `72feb46` |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0.1bp | 100 gp | 1,000 gp | 10,000 gp | 668 | 660 |
| 0.25bp | 250 gp | 2,500 gp | 25,000 gp | 636 | 626 |
| **0.5bp** | **500 gp** | **5,000 gp** | **50,000 gp** | **576** | **575** |
| 1bp | 1,000 gp | 10,000 gp | 100,000 gp | 519 | 521 |
| 2.5bp | 2,500 gp | 25,000 gp | 250,000 gp | 436 | 432 |

Human review selected 0.5bp. It removes rounding-level and small improvements, remains sensitive
in both fixture classes, and sits between materially different 0.25bp and 1bp results without
making zero an arbitrary cliff. The evaluator reports all five sensitivities.

## Archive admission and effective coverage

Both archives were re-imported from the same immutable sources after the expanded
attendance-specific horizons passed.

| Archive | Manifest SHA-256 | Files |
| --- | --- | ---: |
| `2026-07-27T12:19:48Z` | `a61337deaf91d89077c7f9d70d2393ea2ba62c21dbe52ab5c7e38ffb6718e3cf` | 8,525 |
| `2026-07-29T10:38:13Z` | `3ca64ed313cb193658e86fde2083411c4470d9fb1f4f715cbcf1a6597745e9f1` | 8,531 |

| Archive | Patient cutoff/items | Active cutoff/items | Time cutoff/items |
| --- | --- | --- | --- |
| July 27 | Jul 26 11:00 UTC / 406 | Jul 27 07:00 UTC / 5 | Jul 26 00:00 UTC / 487 |
| July 29 | Jul 28 09:00 UTC / 336 | Jul 29 04:20 UTC / 5 | Jul 27 18:00 UTC / 398 |

All 6,412 admitted-item/order-lane/attendance coverage cells pass. This is admitted-item coverage,
not whole-market coverage. In particular, the active-lane observed universe contains only five
items per date.

The 756 matrix cases should not be read as 756 independent market observations:

| Fixture class / lane | Distinct fixture-lane snapshots | Matrix case evaluations |
| --- | ---: | ---: |
| Observed patient | 2 | 108 |
| Observed active | 2 | 36 |
| Observed time | 2 | 72 |
| Synthetic patient | 5 | 270 |
| Synthetic active | 5 | 90 |
| Synthetic time | 5 | 180 |

The larger case counts repeat bankroll, attendance, and slot-cap decisions on the same underlying
fixture-lane snapshot.

## Lane-local planner characterization

Neither planner revision passes the frozen lane-local gates. `72feb46` is a characterization
input, not a compatibility target.

| Metric | `origin/main` (`0adfe382`) | `72feb46` |
| --- | ---: | ---: |
| Cases | 756 | 756 |
| Nonempty cases | 90 | 6 |
| Submitted orders | 120 | 6 |
| Lane-local visible violations | 711 | 575 |
| Cases with a 0.5bp challenger | 576 | 575 |
| Reported nondominated challengers | 30,670 | 30,694 |
| Qualifying challenger alternatives | 11,774 | 11,849 |
| Aggregate submitted withheld utility | -76,527,273 gp | 1,780,581 gp |

For both revisions, the observed cohort has 176 cases with a 0.5bp challenger and zero submitted
withheld utility because no observed orders are submitted. For `origin/main`, the synthetic cohort
has 535 visible violations, including 400 challenger cases, and fails withheld validation at
-76,527,273 gp. Its other submitted-order findings are 72 nonpositive-utility, 30
insufficient-evidence, 28 position-risk, and 5 portfolio-risk violations.

`72feb46` submits six synthetic orders, clears submitted utility/evidence/risk checks, and passes
synthetic aggregate withheld validation at 1,780,581 gp. It still has 399 synthetic challenger
cases. Its lower violation count largely reflects selecting almost nothing, not demonstrated
lane-local selection quality.

These cases share cash and slots only within a lane. The report makes no claim about a balanced
planner's cross-lane allocation.

## Development evidence and fresh validation

Once optimization begins, July 27 and July 29 outcomes are development regression evidence. They
are not a holdout. After the loop, freeze the planner, collect later archives unavailable during
tuning, wait at least the full 24-hour maximum horizon, checksum and preserve them before import,
run the unchanged evaluator once, and report that result separately. If it drives another planner
change, obtain a newer snapshot for the next fresh validation.

## Final baseline

`evaluation/baselines/v2-main.json` was regenerated only after the code/test review and a separate
semantic repeat. It is the 7,293-byte deterministic compact projection of the full ignored result,
records `origin/main`, has status `frozen_v2`, and is not an acceptance allowance.

The former V1 baseline remains `v1-main.json` and is inactive. The V2 identities are:

- contract: `c57da9bec2c67b6a3456e16b789800eb42f6d99dcc7c20e51c77827f18378404`
- evaluator: `ae55e0f7e712bd52a4e78fd1a54a0034a5258430aad3a97a62192746d1e7934c`
- oracle: `b319b6e3b877f87f0d2444be51a3463b86beb01d196bf7e4c01bf6deef4a9ca6`
- admitted-item coverage manifest:
  `e44fa3686a5f0a087d723f61afbe7bbf149f79e2912111b1c8f0122d9e2c8a64`

## Boundary

The lane-local contract and the selected 0.5bp threshold are frozen together. This finalization
changes no planner code, and planner optimization has not begun.

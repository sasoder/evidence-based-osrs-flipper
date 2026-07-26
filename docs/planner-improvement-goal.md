# Planner self-improvement goal

## Outcome

Repair and then improve the deterministic planner until it passes the frozen evaluation contract
across every checked-in market fixture, bankroll, attendance horizon, slot cap, and seed. Improve
withheld-future utility and worst-case performance without optimizing utilization for its own sake.

## Immutable oracle

Do not edit anything under `evaluation/`, `tests/test_evaluation.py`, or the baseline used for the
current loop. Do not weaken or reinterpret an invariant. If the oracle appears wrong, stop and
report the evidence; evaluator changes are a separate human-reviewed task that invalidates every
baseline.

## Required loop

1. Read the baseline report and group violations by cause and affected cases.
2. Choose one small causal planner defect, state the prediction for the patch, and change planner or
   runtime code only.
3. Run `uv run python -m unittest`.
4. Run `uv run python -m evaluation.runner --output evaluation/results/current.json`.
5. During repair, compare with:

   ```bash
   uv run python -m evaluation.compare \
     evaluation/baselines/main.json evaluation/results/current.json --mode repair
   ```

6. Reject the patch if comparison fails. Keep it only if total violations strictly decrease, no new
   violation class appears, aggregate utility improves by the declared margin, and worst-case
   withheld-future profit does not regress.
7. Commit each accepted iteration separately and use that accepted result as the next baseline.
8. Once violations reach zero, use strict comparison and never allow them to return.
9. Stop after five consecutive rejected iterations and report the impasse instead of changing the
   oracle.

## Known causal areas to investigate

- Posted-capital efficiency versus expected-fill accounting.
- Bankroll-scaled floors that remove feasible lower-bankroll books.
- Active exit probability and missing active loss caps.
- Patient selection without a genuinely independent holdout or present-entry fill probability.
- Missing fallback stress loss when a short backtest happens to observe no downside.
- Strategy tags, prediction outcomes, and confidence calibration being discarded from FU history.
- Greedy portfolio allocation using unsized candidate scores instead of portfolio value.
- Full-universe reliability, retry isolation, and diagnostic visibility.

These are hypotheses, not permission to bypass measurement. Fix the cause demonstrated by the
current cases.

## Completion criteria

- All unit tests pass.
- Strict evaluator comparison reports zero hard-invariant violations.
- Aggregate withheld-future utility is higher than the original `main` baseline.
- Worst-case withheld-future profit is no worse than the original baseline and is within configured
  loss limits.
- Expected and actual portfolio value are cash-dominant at every evaluated market/attendance/slot
  combination.
- Strategy selection remains dynamic: the final report retains per-case selected item IDs and shows
  evaluation over multiple distinct portfolios rather than a hard-coded list.
- The final diff receives a fresh review for leakage, overfitting, complexity, and test-oracle
  coupling.

Start this as a new goal only after the evaluator branch is reviewed and committed:

```text
/goal Improve the deterministic planner according to docs/planner-improvement-goal.md. Treat the
frozen evaluator and its baseline as immutable, checkpoint only accepted iterations, and continue
until the documented completion or plateau condition is reached.
```

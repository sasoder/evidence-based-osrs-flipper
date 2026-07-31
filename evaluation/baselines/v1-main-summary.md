# Main planner baseline

This baseline evaluates planner revision `5bb439bb1b11dc64f598dd33de3bc721eb5dc670`
against the frozen version-1 oracle. The machine-readable result is
[`main.json`](main.json).

## Coverage

- 378 cases: 7 market fixtures × 3 attendance modes × 3 slot caps × 6 bankrolls.
- Bankrolls range from 10m to 1b liquid GP.
- The observed fixtures contain all 2,096 eligible items from two separate market dates.
- Five permanent synthetic fixtures cover stable, quiet, reversal, and crash regimes across four
  price tiers and multiple deterministic seeds.
- Each case reruns the production selector from the raw fixture universe before its orders are
  scored on withheld future buckets.

## Dynamic-selection evidence

- 150 distinct selection portfolios were produced.
- 93 of 315 adjacent-bankroll comparisons changed the selected item IDs (29.52%).
- The two observed dates produced 33 and 37 distinct portfolios respectively.

The corpus therefore does not encode a fixed list of “good” items. Market date, liquid GP,
attendance, and available slots all remain inputs to candidate filtering, ranking, and sizing.

## Baseline result

- Mean planner-expected profit: 654,624 gp per case.
- Mean withheld-future simulated profit: -109,599 gp per case.
- Worst case: -6,280,008 gp.
- Aggregate reservation-adjusted utility: -52,938,682 gp.
- 73 case-level violations and 100 cross-bankroll dominance violations.

Violation counts:

| rule | count |
| --- | ---: |
| actual-profit cash dominance | 52 |
| expected-profit cash dominance | 48 |
| position loss | 43 |
| portfolio loss | 16 |
| posted-capital efficiency | 14 |

This is a repair baseline, not an acceptable planner result. The next optimization loop must use
the frozen comparator: reduce violations without adding a new class, improve aggregate utility,
and preserve or improve the worst case. Once violations reach zero, strict mode keeps them there.

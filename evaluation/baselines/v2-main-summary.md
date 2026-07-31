# Frozen Evaluator V2 `origin/main` baseline

This is the final reproducibility baseline for frozen Evaluator V2. It records `origin/main` at
`0adfe38267d0040974b8aa38ec92212c54a7766e`; it is not an acceptance allowance. The approved
challenger-materiality threshold is frozen at 0.5bp. The checked-in JSON is a 7,293-byte compact
projection; the full evaluator output remains under the gitignored `evaluation/results/` directory.

| Metric | Value |
| --- | ---: |
| Cases | 756 |
| Nonempty cases | 90 |
| Lane-local visible violations | 711 |
| Cases with a 0.5bp challenger | 576 |
| Aggregate withheld utility | -76,527,273 gp |
| Observed lane-local gate | Fail |
| Synthetic lane-local gate | Fail |

Both archive provenance audits pass, all 6,412 admitted-item/order-lane/attendance coverage cells
pass, and submitted-order withheld coverage is 100%. The active-lane observed universe is only five
items per date. See [../V2_FINAL_REPORT.md](../V2_FINAL_REPORT.md) for the final review, effective
decision counts, threshold characterization, and comparison with `72feb46`.

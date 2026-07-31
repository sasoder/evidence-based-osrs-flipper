# Frozen Evaluator V2 contract

Evaluator V2 is the frozen standalone oracle for planner optimization. V1 and
`planner-self-improvement` are characterization inputs only; neither defines compatibility or
acceptance. Any change to this contract, its implementation, fixtures, comparison logic, or tests
requires separate human review and baseline regeneration.

The evaluator asks one lane-local question: at a strategy-appropriate decision point, does a
submitted same-lane portfolio use visible evidence better than independently constructed feasible
alternatives, and does that judgment survive withheld validation? It does not assess whole-planner
portfolio optimality across patient, active, and time lanes.

## Decision lanes and scope

Each strategy family is evaluated independently. Cash and slots are shared by orders inside one
lane case only. The evaluator does not create a mixed-lane decision, synchronize different
cadences, or reserve resources across lane cases.

| Decision lane | Planner strategies | Bucket | Base entry | Hold | Attendance |
| --- | --- | ---: | ---: | ---: | --- |
| Patient | patient and probe | 1 hour | 4 hours | 12 hours | attended, away 2h, overnight |
| Active | active | 5 minutes | 30 minutes | 90 minutes | attended |
| Time | time of day | 6 hours | 6 hours | 24 hours | attended, away 2h |

Patient and patient-probe orders compete for cash and slots inside the patient case and retain
separate participation rates. Calling the resulting gate a whole-planner or balanced-strategy
portfolio-optimality gate would be incorrect.

## Visible boundary and admitted universe

Every fixture records a cutoff for each decision lane. The evaluator, independently of planner and
signal code, admits member items with a positive GE limit and at least three complete contiguous
replay horizons ending exactly at the cutoff. History length uses the longest allowed
attendance-specific horizon.

The planner receives only the admitted lane universe, that lane's history, and quotes derived from
the final visible bucket. It never receives future rows, outcome or coverage metadata, another
lane's later history, or a fixture-provided ranking. Evaluator selection and replay import no
planner, signal, or planner-tax implementation.

## Attendance and chronological replay

Planner prose, strategy tags, confidence, and expected profit do not determine replay semantics.
The structural output section selects the evaluator order lane. Executable identity is:

`item_id, side, quantity, buy_price, sell_target, cancel_after, hard_exit_after,
management_after`.

Attendance is executable semantics. `management_after` is the first instant at which the player
can manage an offer. Cancellation is the later of the base entry window and the player's return;
hard exit adds the lane hold window after cancellation.

| Patient attendance | Management starts | Entry cancellation | Hard exit |
| --- | ---: | ---: | ---: |
| Attended | 0h | 4h | 16h |
| Away 2h | 2h | 4h | 16h |
| Overnight | 12h | 12h | 24h |

An overnight patient offer therefore cannot be cancelled, replaced, or converted to a sell offer
while the player is away. Challenger “replacement” means a counterfactual alternative submitted
at the original cutoff; replay never performs an in-horizon cancel-and-replace operation.

Replay proceeds bucket by bucket:

1. A resting buy fills only on a low-side touch, capped by configured low-side participation.
2. A single GE slot cannot sell partial inventory while its buy remains open.
3. Target selling starts only after a full fill or entry cancellation, in a later bucket, and never
   before `management_after`.
4. Target sales require a high-side touch and are capped by sell-side participation.
5. Remaining inventory is force-sold at the final low-side price and taxed.
6. Posted cash is reserved from the decision time; unfilled cash releases at cancellation and
   filled cash remains charged until chronological sale or forced exit.

Aggregate touch/capacity shortcuts are not part of V2.

## Visible scoring, evidence, and materiality

Submitted orders and evaluator challengers use the same replay. Visible utility is:

`realized replay profit - posted-capital hours × reservation rate`.

Evidence requires at least three non-overlapping replay horizons and two distinct entry-opportunity
episodes. Continuous touches form one episode. Position risk is the worst visible replay loss;
portfolio risk is the worst synchronized loss inside the decision lane.

A challenger is eligible only when its exact mean incremental visible utility is positive, each
changed action clears the evidence floor, the alternative clears position and lane-portfolio risk,
and the whole alternative satisfies cash, slot, uniqueness, admitted-universe, GE-limit, side, and
quantity constraints.

Eligibility alone is not a violation. Exact mean delta must meet
`max(1 gp, ceil(bankroll × fraction))`. V2 reports five candidate thresholds:

| Label | Bankroll fraction | 10m | 100m | 1b |
| --- | ---: | ---: | ---: | ---: |
| 0.1bp | 0.001% | 100 gp | 1,000 gp | 10,000 gp |
| 0.25bp | 0.0025% | 250 gp | 2,500 gp | 25,000 gp |
| 0.5bp | 0.005% | 500 gp | 5,000 gp | 50,000 gp |
| 1bp | 0.01% | 1,000 gp | 10,000 gp | 100,000 gp |
| 2.5bp | 0.025% | 2,500 gp | 25,000 gp | 250,000 gp |

The frozen gate uses the human-approved 0.5bp threshold. It suppresses rounding-level and
economically immaterial improvements while retaining materially more sensitivity than 1bp in both
fixture classes. A positive fractional delta below the selected floor is reported but cannot fail
a planner.

Unspent cash and unused slots are not violations by themselves. They matter only when a
materially qualifying feasible challenger can use them.

## Complete cash-aware Pareto frontier

For each fixture, lane, attendance, and bankroll, the evaluator constructs canonical actions from
visible buckets and evaluates every feasible calculated integer breakpoint around fill capacity,
target capacity, forced exits, utility, risk, affordability, and GE limits.

Per-item reduction retains every nondominated quantity. Global reduction retains every
still-nondominated action across lower posted cash, higher exact visible utility, more replay
blocks, more opportunity episodes, and lower maximum visible loss. Dominance uses exact rational
utility, not rounded display gp. There is no item-count cap.

An invariant test independently exhausts all generated breakpoint candidates and requires the
staged returned frontier to equal the exhaustive nondominated set. Additions, replacements,
resizes, and a deterministic benchmark are checked as complete lane-local portfolios, then
Pareto-reduced without a cardinality cutoff.

## Withheld validation and admitted-item coverage

Withheld buckets are absent from universe selection, action construction, quantity selection,
frontier dominance, visible scoring, and challenger qualification. They attach only after visible
records are fixed.

Coverage is keyed by fixture, admitted item, executable order lane, attendance, and cutoff. It
requires all bucket fields, exact spacing, the first bucket immediately after cutoff, and the full
attendance-specific horizon. This is admitted-item coverage, not whole-market coverage.

## Corpus admission and future validation

The development corpus contains five deterministic synthetic fixtures and the checksum-verified
July 27 and July 29 cache archives. An observed archive is imported only after its manifest exactly
covers every cache JSON file, every file matches its checksum, provenance is recorded, each
admitted item has complete visible and withheld horizons, the frozen lane item floor passes, and
the runtime audit agrees.

Observed floors are 25 patient, 5 active, and 25 time items. The admitted active-lane universe is
exactly five items on each observed date. This is a narrow active sample, not broad
observed-market coverage. Reports distinguish distinct fixture/lane snapshots from repeated
bankroll/attendance/slot matrix cases and show admitted items per fixture.

Once planner optimization starts, July 27 and July 29 outcomes are development regression evidence,
not a holdout. Post-loop validation must:

1. freeze the planner artifact after the loop;
2. collect later immutable archives unavailable during tuning;
3. wait for the longest future horizon, currently 24 hours;
4. checksum and preserve the archives before import;
5. import with the unchanged evaluator and run the frozen planner once; and
6. report the fresh result separately.

If that result informs another planner change, it becomes development evidence and a newer
snapshot is required for another fresh validation.

## Separate lane-local gates

Observed and synthetic cohorts are reported and gated separately. Each lane-local cohort gate
requires fixture provenance, complete admitted-item coverage, no submitted visible-score,
feasibility, evidence, or risk violations, no materially qualifying visible challenger, and full
submitted-order withheld coverage with nonnegative aggregate withheld utility.

Both cohort gates must pass. They do not imply cross-lane shared-cash/shared-slot optimality. V2
does not impose utilization, expected-profit, or per-case realized cash-dominance requirements.

## Baseline and immutability

`evaluation/baselines/v2-main.json` is the final V2 reproducibility record, not an acceptance
allowance. It is a deterministic compact projection; full evaluator outputs remain under the
gitignored `evaluation/results/` directory. Comparison requires identical contract, evaluator,
oracle, and fixture hashes, then depends on candidate lane-local gates rather than baseline
performance.

Any evaluator, fixture, test, or contract change invalidates the baseline and requires review plus
regeneration. The lane-local contract and 0.5bp materiality threshold are frozen together.

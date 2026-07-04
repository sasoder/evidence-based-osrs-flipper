---
name: flip
description: Run an on-demand OSRS GE flipping planning session. Use when the user asks what to buy, asks for a flip plan, or asks for merchanting advice from Flipping Utilities exports and merch.plan.
---

# Flip Planning Workflow

The engine-agnostic runtime workflow for a flip planning session. The output of a session is
exact GE instructions the user executes manually in-game. Read `README.md` for setup.

## Runtime workflow (a planning session)

During a session, do **not** read the bank, full diagnostics, or candidate dumps; use only the
commands and outputs listed below.

### 0. Collect the user's inputs

Parse the user's message first — never re-ask anything already stated. Skip straight to step 1
only when every needed new-buy input is either stated or intentionally selected in the intake:
liquid gp, attendance, strategies, and slot cap. Do **not** silently omit strategies or slots
from the intake just because they have defaults; the user should see and accept those defaults.
Liquid gp alone is not enough: attendance changes which strategies are safe, so a bare
"30m, what should I buy" still gets the intake prompt.

Exception — a pure offer-review request ("what should I do with my offers?", "look at my
slots") needs no intake at all: run `merch.plan --cash 0 --max-new-slots 0 --markdown` and
present the open-offer checks. Ask for the full new-buy intake only if the user then wants new
buys. Collect all missing inputs in one round (a single structured multi-question prompt if the
engine supports one, otherwise one concise message):

- **Liquid gp** (required): suggested options `10m` / `50m` / `100m` / `250m`, exact amounts
  welcome. Pass the exact figure to `--cash`. Liquid gp is always reported by the user because
  it is the amount they want this run to deploy.
- **Attendance**: `At keyboard — can manage offers (default)` → no flag; `Away a few hours` →
  ask roughly how long and pass `--away-hours <n>`; `Away 8h+ / sleeping` → `--horizon
  overnight`. Attendance is a planner constraint like strategies and slots: the planner itself
  disables any strategy whose offers need management sooner than the user returns (active needs a
  cancel decision within 30 minutes) and records the exclusion. `--away-hours 8`+ implies
  overnight, which also sizes patient to the 12h window. Never add or remove strategies by hand to
  compensate for attendance — pass the absence to the planner.
- **Strategies** (multi-select): `Balanced — all of the below (default)` / `Patient` /
  `Active` / `Time-of-day` / `Probe`. Strategy sets are unioned, so Balanced absorbs any other
  selection; if it is picked, pass `--strategies balanced` and ignore the rest. Otherwise join
  the individual picks with commas for `--strategies`. The `conservative` preset remains valid
  as typed input.
  An active-only request implies at-keyboard; don't ask attendance.
- **Slots**: `All free (default)` / `2` / `4`, exact counts welcome → `--max-new-slots`.

Contradiction guard: any stated absence disables the active strategy, so active-only + away errors
out of the planner with the reason — relay it and re-ask instead of hand-editing strategies. If the
user changes strategies or attendance mid-session, rerun the planner with both current values; a strategy
request never overrides a previously stated absence.

### 1. Collect live inputs

Refresh RuneLite exports before reading offers:

```bash
scripts/runelite-sync.sh
uv run python -m merch.runelite offers
```

If that returns nothing or looks stale/missing, ask the user for current open GE offers (slot,
item, side, qty, filled qty, price, state, age, and time since the last fill), formatted as JSON
for the planner:
`[{"slot":0,"id":11212,"side":"sell","qty":11000,"filled_qty":0,"price":3390,"age_hours":6.5,"state":"ACTIVE"}]`.
Offer age matters: stale sells are measured from their last fill; zero-fill buys older than 4h
should be cancelled rather than chased. Do not assume what's true since the last run, and never
size off a stale snapshot.

### 2. Fast deterministic plan (default)

```bash
uv run python -m merch.plan --cash <liquid_gp> --write-intents --markdown
```

If the user gave preferences, pass them directly:

```bash
uv run python -m merch.plan --cash <liquid_gp> \
    --strategies patient,active --max-new-slots 3 --write-intents --markdown
```

Strategy and slot preferences are planner constraints, not LLM discretion. Do not add or remove rows
by hand after the planner returns. For overnight requests, run `merch.plan --horizon overnight
--seed-limit 0 --time-seed-limit 0`; this excludes active-margin calls before sizing and intent
writing, sizes patient candidates to the 12h hold window, and also checks the account's best FU
round-trip items even when they are outside the current margin top-N. Do not manually remove
active rows after the final plan.

`--seed-limit 0` and `--time-seed-limit 0` scan every liquid item; use them when completeness
matters. Do not run `signals scan`, `signals backtest`, `active-scan`, `time-scan`, or ad hoc
JSON slicing during a session; the planner already performed those checks. In the normal path,
run the planner once with `--write-intents --markdown` and present the result.

### 3. Optional slower research overlay

Skip research unless the user asks for catalysts/news or the request is clearly event-driven.
If triggered, follow `research.md` (in this skill's directory) exactly. Research never bypasses
the strategy gate.

### 4. Present + tag intent

- Present the planner markdown unchanged, including its deployment utilization, plus a short
  FU-history note only when you have explicit FU stats to cite. Do not invent
  grading/accountability summaries from repo state.
- Then interpret it: for each row the user asks about, explain the verdict in plain terms using
  the row's live lo/hi, break-even, and quantified alternatives. "Untracked offer" means the
  outcome won't be strategy-graded — the advice still applies in full; never present an
  untracked row as "not my problem".
- `--write-intents` writes the thin pending queue consumed by the FU fork. It contains only
  exact offer signatures plus strategy/reason/prediction tags; FU remains authoritative for
  whether the offer was placed, filled, cancelled, and profitable.
- Do not ask the user to confirm that they placed the offers. The FU fork observes matching
  manual offers from the current-slot export and trade history. Ask only when a later run sees
  stale/missing FU data or an observed offer does not match the exact intent.
- Never write intents for actions you tell the user to skip. If a row is unsafe for the stated
  horizon, the planner invocation is wrong; rerun with the correct horizon rather than filtering
  the markdown by hand.
- Commit a terse one-line summary only if the user asks for persistence in Git.

## Where the LLM is — and isn't

The decision loop is deterministic. `merch.plan` grades past fills, scans candidates, applies
strategy gates, triages open offers, sizes against budget/slots, and writes each call's reason
and falsifiable prediction. The LLM may interpret catalyst research into a small
`{boost, avoid}` overlay that `merch.plan --overlay` consumes. Do not re-do in the LLM what
the planner already did (ranking, sizing, triage, formatting); feed the reasoning step only the
research digest plus the candidate names. Boost only re-ranks gate survivors; avoid vetoes one.
Research never creates an otherwise ineligible trade.
During `/flip`, do not run separate scan/backtest CLIs or repeatedly rerun `merch.plan` to inspect
sections; the final markdown action table is the executable contract.

## State model

Flipping Utilities is the source of truth for GE activity: current offers, fills, realized
profit, and trade history. Liquid GP is provided by the user at the start of each `/flip` run and is
the amount the planner may size against. Do not maintain a parallel short-term position registry
in this repo.

The only repo-side trade memory should be thin planner intent: item, side, qty, limit price,
strategy label, reason, horizon, and falsifiable prediction. Intent labels explain why an offer
was suggested; they are not the transaction database and never override FU fills.
After writing intents, do not ask the user to confirm placed offers. The FU fork should observe exact
manual offers and remove matching pending intents; ask only when FU data is stale/missing or the
observed offer differs from the exact signature.

Long-term holds are out of scope for the day-to-day `/flip` loop. If the user wants a thesis note
for a non-trading position, write it as a simple note/report, not as state that blocks normal
market scanning.

## The prime rule — no action without a reason

Every recommendation records: bucket, qty, price, reason, time horizon, a *falsifiable*
prediction (direction + target + by-date), confidence, and a thin intent record. If you can't
state a reason and a prediction, don't recommend it.

**Every order is exact:** one item, one side, one integer quantity, one limit price — never a
range. Exactness is what lets FU/plugin tags or the intent matcher attach the strategy label to
the actual offer. Vague orders can't be evaluated.

## Selection is the edge — strategy-specific gates

A fat paper margin usually means the item has a wide range because it is *trending down*, not
oscillating. Execution bands use 1h data; the broader regime guard uses 6h data. `merch.plan`
runs the patient-band backtest gate internally before recommending a buy. Reject any candidate
that is not at a fresh live-low entry, is not net-positive (`total_profit_per_unit > 0`) with
enough round-trips (`trades >= 4`) under that gate. Rank survivors by expected realized gp/hour,
not paper margin.

Near-band bids that have not actually traded at the buy band are never promoted into the normal
strategy. They may enter a separate **`flip-patient-probe`** experiment only when the live instant-sell
print is within 3% above the band, the normal survival/regime gates pass, and expected realized
profit clears the slot floor. This strategy uses at most 5% of liquid across all probe offers. It
exists to gather real fill evidence; its distance rule is not backtest-validated and never weakens
the production fresh-live-low gate.

Daily UTC patterns use a separate **`flip-time-of-day`** experiment. `merch.signals time-scan`
tests 6-hour entry/exit windows using roughly three months of data: the older 70% selects the
window and the newest 30% must independently remain net-positive with at least 20 observations,
a positive median after-tax profit, and at least a 60% win rate; the training window needs at
least 40 observations. This strategy sizes to requested/free slots, fillability, GE limit, available
liquid, and the normal slot-profit floor, cancels a zero-fill entry after its 6-hour UTC window,
and hard-exits by 24h. It never weakens the normal patient-band gate or gets merged into patient
performance.

High-value gear uses a separate **`flip-active`** strategy because ordinary 15-90 minute margin
flipping is not the percentile-band strategy. `merch.signals active-scan` screens items above
1m for fresh two-sided prints, after-tax net margin, minimum ROI, real flow on both sides, and
no sharp 5m decline. Active quantity is capped only by the GE limit and available liquid gp.
Active offers compete for available slots by expected realized gp/hour, cancel if unfilled after
30 minutes, and hard-exit by 90 minutes.
Do not claim the 12h band backtest validates these calls; label and grade them separately.

An item becomes a **staple** only after at least five profitable completed round-trips, positive
aggregate realized profit, and a median round-trip time no greater than 12h. Staple status is
execution evidence and may raise confidence; it never bypasses freshness, regime, backtest,
liquidity, budget, or slot gates.

## Sizing & deployment

- Liquid gp is the amount the user explicitly wants deployed this run, so the budget is all of
  it: deploy as much as the gates allow within the user's requested/free slots. Leaving more
  than 1% unspent must come with the blocking constraint stated; never weaken gates merely to
  hit full utilization.
- The user may constrain the run with planner flags such as `--strategies patient,active` and
  `--max-new-slots 3`. Treat those as deterministic constraints. Do not add disabled strategies back
  by hand, and do not exceed the slot cap to improve utilization.
- Size every offer to at most the signal's `fillable_qty` (estimated fills over the next 4h).
  A thin item (low `score` driven by small
  `liquidity_profit`) stays small or unfilled regardless of margin.
- The default patient flip horizon is 2-6h with a hard 12h exit. Never reprice a buy upward. Cancel
  zero-fill buys after 4h; clear stale sells toward the live bid after 6h; at 12h the stop-loss
  may realize a loss because the backtest books the same forced exit.
- Patient probes use the same 4h zero-fill cancellation and 12h hard exit, but stay capped at 5%
  of liquid across all probe offers. Report them separately from validated patient-band buys.
- Time-of-day experiments size to requested/free slots, fillability, GE limit, and available
  liquid, cancel zero-fill buys after 6h, and hard-exit by 24h. Report and grade them separately.
- Active-margin probes use a 15-90 minute horizon and cancel zero-fill buys after 30 minutes.
  Quantity is capped by the GE limit, available liquid gp, and explicit slot constraints.
- Never spend beyond the manually reported liquid gp; never invent low-quality trades to fill
  slots. If the planner uses fewer offers than requested, state the blocking constraint and next
  step (wait, reduce liquid GP, or accept fewer/lower-confidence slots).
- Treat `ready_to_buy=false`, `price_fresh=false`, `blocked_by`, or `regime.level=high` as a
  reason to wait unless research gives a clear thesis.

## Data discipline

- **Right-size every LLM-bound payload.** `merch.plan` is the compact gather-and-decide entry
  point. Never dump a whole dataset into context — the price CLIs reduce by default and refuse
  bare universe dumps; filter by id/`--ids`/name.
- **Never infer cash from snapshots.** Ask the user for current liquid GP and pass it as
  `merch.plan --cash <liquid_gp>` every run. This can intentionally be less than account cash if
  they want to reserve GP outside the harness.
- When discussing the user's open offers, `merch.prices mapping <id>` and
  `merch.prices latest <id>` are fair game for those items — name resolution and a current
  quote are part of giving a real answer, not a dataset dump. The universe-wide scan CLIs
  remain off-limits during a session.
- External research is optional in `/flip` because it often adds latency without changing gated
  survivors. Run `uv run python -m merch.research brief` only when the user asks for
  research/catalysts/news or the request is clearly event-driven; the full procedure lives in
  this skill's `research.md`. If used, it never fails silently:
  an unreachable source returns a concrete `error` (e.g. `HTTP 403`) you must cite — never a vague
  "web checks unavailable".

## Accountability

- Grade from **real FU fills, not market drift**. FU profit/history is authoritative for what
  happened; planner intents only supply strategy attribution and the prediction to compare
  against.
- Keep strategies separate: patient-band, patient-probe, active-margin, time-of-day, manual,
  and liquidation should not be mixed when measuring realized gp/hour.
- Track rolling hit-rate and calibration (did 60%-confidence calls hit ~60%?). Patterns that
  repeatedly miss get demoted; patterns that work get more capital. State misses plainly.

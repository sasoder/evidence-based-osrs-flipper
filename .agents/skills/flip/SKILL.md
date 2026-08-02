---
name: flip
description: Run an on-demand OSRS GE flipping planning session. Use when the user asks what to buy, asks for a flip plan, or asks for merchanting advice from RuneLite data and flipper.plan.
---

# Flip Planning Workflow

The engine-agnostic runtime workflow for a flip planning session. The output of a session is
exact GE instructions the user executes manually in-game. Read `README.md` for setup.
Commands below use the recommended `uv` runner. If setup selected an activated Python 3.11+
environment instead, omit `uv run`.

## Runtime workflow (a planning session)

During a session, do **not** read the bank, full diagnostics, or candidate dumps; use only the
commands and outputs listed below.

### 0. Collect the user's inputs

Parse the user's message first — never re-ask anything already stated. Skip straight to step 1
only when every needed new-buy input is either stated or intentionally selected in the intake:
spendable gp, attendance, strategies, and slot cap. Do **not** silently omit strategies or slots
from the intake just because they have defaults; the user should see and accept those defaults.
Liquid gp alone is not enough: attendance changes which strategies are safe, so a bare
"30m, what should I buy" still gets the intake prompt.

Exception — a pure offer-review request ("what should I do with my offers?", "look at my
slots") needs no intake at all: run `flipper.plan --cash 0 --max-new-slots 0 --markdown` and
present the open-offer checks. Ask for the full new-buy intake only if the user then wants new
buys. Collect all missing inputs in one round (a single structured multi-question prompt if the
engine supports one, otherwise one concise message):

- **Spendable gp outside GE offers** (required): suggested options `10m` / `50m` / `100m` /
  `250m`, exact amounts welcome. Pass the exact figure to `--cash`; the planner automatically adds
  proceeds/refunds released by collect/cancel actions in this plan.
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

Refresh and reconcile RuneLite state before reading offers:

```bash
uv run python -m flipper.sync
uv run python -m flipper.runelite offers
```

An empty list is valid when every slot is empty. Fresh stock Flipping Utilities data drives slot
presence/fills/timing; RuneLite core confirms prices, while a matched intent price is provisional
and must be labeled as such until confirmation. If the snapshot is missing or profile discovery
fails, repair setup before planning. Only ask the user for current open GE offers (slot,
item, side, qty, filled qty, price, state, age, and time since the last fill), formatted as JSON
for the planner:
`[{"slot":0,"id":11212,"side":"sell","qty":11000,"filled_qty":0,"price":3390,"age_hours":6.5,"state":"ACTIVE"}]`.
Offer age matters: stale sells are measured from their last fill; zero-fill buys older than 4h
should be cancelled rather than chased. Do not assume what's true since the last run, and never
size off a stale snapshot.

Partial fills are positions, not a blanket cancel signal. Hold the remainder while its strategy's
entry is still valid. If the deterministic triage independently cancels or reprices a partially
filled buy, cancel the unfilled remainder and list exactly the acquired quantity; a GE buy cannot
be edited in place. A tracked buy collected between syncs is recovered from terminal history and
listed unless that item is already represented by a current offer.

### 2. Fast deterministic plan (default)

```bash
uv run python -m flipper.plan --cash <spendable_gp> --write-intents --markdown
```

If the user gave preferences, pass them directly:

```bash
uv run python -m flipper.plan --cash <spendable_gp> \
    --strategies patient,active --max-new-slots 3 --write-intents --markdown
```

When the user explicitly asks about their usual, historical, staple, previously successful, or
previously well-performing items, also pass `--report-personal-history`. This adds a Markdown
section for historical candidates that did not clear today's checks. Do not pass it for an
ordinary plan; personal FU evidence may still widen and size the candidate pool without adding
diagnostic noise to the result.

Strategy and slot preferences are planner constraints, not LLM discretion. Do not add or remove rows
by hand after the planner returns. For overnight requests, run `flipper.plan --horizon overnight
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
  performance summaries from repo state.
- Then interpret it: for each row the user asks about, explain the verdict in plain terms using
  the row's live lo/hi, break-even, and quantified alternatives. "Untracked offer" means the
  harness did not bind strategy context — the advice still applies in full; never present an
  untracked row as "not my problem".
- `--write-intents` writes the harness's thin pending queue. It contains the item/side/quantity
  order identity, intended price, strategy, reason, and any hard exit. The next sync binds a
  matching RuneLite offer and consumes the pending entry; a different price alone is not a mismatch.
- Do not ask the user to confirm that they placed the offers. The next fresh FU autosave normally
  observes them within one minute, with core RuneLite as the slower fallback. Ask only when neither
  source can establish the offer or an observed offer remains untagged.
- Never write intents for actions you tell the user to skip. If a row is unsafe for the stated
  horizon, the planner invocation is wrong; rerun with the correct horizon rather than filtering
  the markdown by hand.
- Commit a terse one-line summary only if the user asks for persistence in Git.

## Where the LLM is — and isn't

The decision loop is deterministic. `flipper.plan` uses past fills, scans candidates, applies
strategy gates, triages open offers, sizes against budget/slots, and writes each call's reason.
The LLM may interpret catalyst research into a small
`{boost, avoid}` overlay that `flipper.plan --overlay` consumes. Do not re-do in the LLM what
the planner already did (ranking, sizing, triage, formatting); feed the reasoning step only the
research digest plus the candidate names. Boost only re-ranks gate survivors; avoid vetoes one.
Research never creates an otherwise ineligible trade.
During `/flip`, do not run separate scan/backtest CLIs or repeatedly rerun `flipper.plan` to inspect
sections; the final markdown action table is the executable contract.

## State model

Fresh stock Flipping Utilities is the preferred source for current slot presence, quantities,
fills, and timestamps. RuneLite's built-in Grand Exchange state confirms exact limit prices and
terminal history after its batched disk write. A matched intent price may be used provisionally and
must be disclosed; it is replaced by the core price when available. The harness owns intent
bindings and observation state. Spendable GP outside GE offers is provided by the user at the
start of each `/flip` run; the planner adds only cash released by actions in that plan.

Repo-side state stays narrow: current offer identity and observed fill quantity plus thin planner
intent (item, side, qty, limit price, strategy label, reason, creation time, and any hard exit).
Intent labels explain why an offer was suggested; they never override RuneLite fills.
After writing intents, do not ask the user to confirm placed offers. The next sync identifies manual
offers by item, side, and quantity and removes the matching pending intent. Price differences do not
prevent tagging; when otherwise identical intents are pending, intended price breaks the tie. Ask
only when RuneLite data is unavailable or the observed offer remains untagged.

Long-term holds are out of scope for the day-to-day `/flip` loop. If the user wants a thesis note
for a non-trading position, write it as a simple note/report, not as state that blocks normal
market scanning.

## The prime rule — no action without a reason

Every recommendation records its bucket, quantity, price, reason, time horizon, deadline, and thin
intent record. If you cannot state a concrete reason and exit plan, do not recommend it.

**Every order is exact:** one item, one side, one integer quantity, one limit price — never a
range. This makes the recommendation executable and its outcome measurable. The harness's matching
contract is narrower: item, side, and quantity identify the order, while price differences do not
prevent the strategy label from attaching. Vague recommendations still can't be evaluated.

## Selection is the edge — strategy-specific gates

A fat paper margin usually means the item has a wide range because it is *trending down*, not
oscillating. Execution bands use 1h data; the broader regime guard uses 6h data when enough of it
exists. `flipper.plan` replays the exact executable patient order through recent non-overlapping
entry/hold blocks, with touch capacity, capital reservation, and forced exits. Production requires
a fresh live low no more than 2.5% above the buy band, at least three replay blocks and two distinct
entry episodes, positive replay utility, and a non-high regime. Rank survivors by expected realized
gp/hour, not paper margin.

When the live instant-sell print is more than 2.5% but no more than 3% above the buy band, the item
may enter a separate **`flip-patient-probe`** experiment. The probe posts at the lower band price,
replays that same band order, must pass the normal replay/regime/profit gates, and uses at most 5%
of liquid across all probe offers. It exists to gather real evidence that the band bid can fill;
the distance rule never weakens the production gate.

Daily UTC patterns use a separate **`flip-time-of-day`** experiment. `flipper.signals time-scan`
tests 6-hour entry/exit windows using roughly three months of data: the older 70% selects the
window and the newest 30% must independently remain net-positive with at least 12 observations,
a positive median after-tax profit, and at least a 60% win rate; the training window needs at
least 40 observations. Today's exact live buy/sell pair must also pass a capacity-aware replay with
capital reservation and forced exits. This strategy sizes to requested/free slots, fillability, GE
limit, available liquid, replayed downside, and the normal slot-profit floor; it cancels a zero-fill
entry after its 6-hour UTC window and hard-exits by 24h. It never weakens the normal patient-band
gate or gets merged into patient performance.

High-value gear uses a separate **`flip-active`** strategy because ordinary 15-90 minute margin
flipping is not the percentile-band strategy. `flipper.signals active-scan` screens items above
1m for fresh two-sided prints, after-tax net margin, minimum ROI, real flow on both sides, and
no sharp 5m decline. The executable pair must produce at least three completed replay trades from
two entry episodes, at least a 60% win rate, positive mean profit, and enough edge to cover its
worst replayed loss. Active quantity is capped by expected fills, the GE limit, available liquid,
and the strategy-wide forced-exit risk budget. Active offers compete for available slots by expected
realized gp/hour, cancel if unfilled after 30 minutes, and hard-exit by 90 minutes.
Do not claim the 12h band backtest validates these calls; label and grade them separately.

An item becomes a **staple** only after at least five profitable completed round-trips, positive
aggregate realized profit, and a median round-trip time no greater than 12h. Staple status is
execution evidence worth citing; it never bypasses freshness, regime, backtest,
liquidity, budget, or slot gates.

## Sizing & deployment

- Spendable gp is the cash currently outside GE offers. The planner adds proceeds and refunds from
  collect/cancel actions it recommends, then deploys as much of that effective budget as the gates
  allow within the user's requested/free slots. Leaving more
  than 1% unspent must come with the blocking constraint stated; never weaken gates merely to
  hit full utilization.
- The user may constrain the run with planner flags such as `--strategies patient,active` and
  `--max-new-slots 3`. Treat those as deterministic constraints. Do not add disabled strategies back
  by hand, and do not exceed the slot cap to improve utilization.
- Every strategy sizes to the conservative `fillable_qty` expected-fill estimate, capped by budget
  and GE limit, so unlikely fills are never credited. Patient and time-of-day positions are
  additionally capped by what a replayed forced exit would cost; active positions draw on a single
  strategy-wide forced-exit risk budget shared across every active slot the run opens.
- Every slot must clear two floors: a flat per-slot floor (1,000gp — absolute, because what one
  offer can earn is capped by the item's buy limit and flow, not by the bank) and a capital-return
  floor (0.05%/hour on the gp expected to be committed to the round trip).
  A thin item (low `score` driven by small `liquidity_profit`) stays small or unfilled
  regardless of margin.
- The default patient flip horizon is 2-6h with a hard 12h exit. Never reprice a buy upward. Cancel
  zero-fill buys after 4h; clear stale sells toward the live bid after 6h; at 12h the stop-loss
  may realize a loss because the backtest books the same forced exit.
- Patient probes use the same 4h zero-fill cancellation and 12h hard exit, but stay capped at 5%
  of liquid across all probe offers. Report them separately from validated patient-band buys.
- Time-of-day experiments size to requested/free slots, fillability, GE limit, and available
  liquid, cancel zero-fill buys after 6h, and hard-exit by 24h. Report and grade them separately.
- Active-margin probes use a 15-90 minute horizon and cancel zero-fill buys after 30 minutes.
  Quantity is capped by the GE limit, deployable gp, and explicit slot constraints.
- Never spend beyond the effective budget; never invent low-quality trades to fill slots. If the
  planner uses fewer offers than requested, state the blocking constraint and next step (wait,
  reduce spendable GP, or accept fewer slots).
- Cancelled-buy refunds and collected-sale proceeds are automatically included only when the plan
  itself instructs those release actions. Never count money from an offer it keeps open.
- A filled-but-uncollected sell is a `collect` action. Its slot is available after that action and
  its net proceeds are included in the effective budget for the same run.
- A `personal_unevaluated[].blocked_by` diagnostic or `price_fresh=false` means wait; research
  cannot repair missing data or a non-positive spread. `ready_to_buy=false` excludes a production
  patient buy, though `patient_probe_ready=true` may still admit the separate probe strategy.
  `regime.level=high` requires an explicit research boost before the deterministic gate admits it.

## Data discipline

- **Right-size every LLM-bound payload.** `flipper.plan` is the compact gather-and-decide entry
  point. Never dump a whole dataset into context — the price CLIs reduce by default and refuse
  bare universe dumps; filter by id/`--ids`/name.
- **Never infer cash outside the GE.** Ask the user for currently spendable GP and pass it as
  `flipper.plan --cash <gp>` every run. The planner derives only cash released by its own GE actions.
- When discussing the user's open offers, `flipper.prices mapping <id>` and
  `flipper.prices latest <id>` are fair game for those items — name resolution and a current
  quote are part of giving a real answer, not a dataset dump. The universe-wide scan CLIs
  remain off-limits during a session.
- External research is optional in `/flip` because it often adds latency without changing gated
  survivors. Run `uv run python -m flipper.research brief` only when the user asks for
  research/catalysts/news or the request is clearly event-driven; the full procedure lives in
  this skill's `research.md`. If used, it never fails silently:
  an unreachable source returns a concrete `error` (e.g. `HTTP 403`) you must cite — never a vague
  "web checks unavailable".

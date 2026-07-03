# Evidence-Based Flipping

**An OSRS Grand Exchange flipping advisor.** It reads your Flipping Utilities data, checks OSRS
Wiki prices, and gives you exact buy/sell/cancel/reprice instructions to type into the GE. It
never touches the game: you place every offer yourself.

Every recommendation records a reason and a falsifiable prediction (direction, target,
deadline), and later runs grade those predictions against what actually filled. Your own
execution history feeds back in: an item only counts as a personal staple after five profitable
round trips, and staples get sized with more confidence.

Inspired by Leverage In Action's
["I Tried Using Data Science to Profit in a Video Game Economy"](https://www.youtube.com/watch?v=FhTLApOoWX8).

## What you can do with it

You use it through any coding agent that supports Agent Skills (the `flip` skill lives in
`.agents/skills/`, `.claude/skills/` holds a thin pointer to it). Typical requests:

- **"50m liquid, what should I do?"** It syncs your RuneLite exports, triages every open offer
  (hold, cancel, collect, or reprice), then fills your free slots with new calls and presents one
  action table.
- **"Going to bed, 120m."** Overnight mode: drops the strategies that need you at the keyboard
  and sizes positions to a 12-hour window.
- **"Only active flips, max 5 slots."** Strategy and slot preferences go straight to the planner
  as hard constraints.
- **"Are my best items from the past month still worth flipping?"** Your own trade history is
  part of the data. Proven round-trip items carry extra weight, and overnight plans check them
  even when current margins wouldn't surface them.
- **"Anything being talked about right now that's worth flipping?"** Optional research pass:
  reads the official OSRS news feed plus r/2007scape and r/OSRSflipping (configurable), then
  boosts or vetoes candidates. Research can re-rank what already passed the gates but can't
  invent a trade that didn't.

If your message doesn't include the numbers, the agent asks for them: liquid GP, whether you'll
be around to manage offers, which strategies to run, and a slot cap. A plan looks like this:

| action | item | qty | price | deadline |
|---|---|---:|---:|---|
| reprice sell | Ornate maul handle | 12 | 747,005 | |
| collect | Stymphike feather | 5,000 | 1,789 | |
| buy | Halibut | 958 | 2,147 | 21:56 UTC |
| buy | Accursed sceptre (u) | 7 | 6,491,874 | 23:26 UTC |

Every row also carries its reason (for row 4: fresh two-sided prints, 318k/unit net spread after
tax, 36 buys/35 sells in the last hour, cancel unfilled after 30m, hard exit by 90m).

## How it decides

The LLM does not pick trades. A deterministic planner (`merch.plan`) does the ranking, sizing,
triage, and formatting; the agent collects your inputs, runs it once, and reads the result back.

Buy and sell targets come from percentile bands over about 15 days of hourly price data (buy at
the 35th percentile of instant-sells, sell at the 75th of instant-buys), and every margin is
after GE tax. Open offers are triaged before anything new is suggested, with explicit stale
rules: zero-fill buys are cancelled after 4h, stale sells walk toward the live bid, and the 12h
forced exit gets booked even at a loss, because the backtest books the same exit and holding
past it would be grading dishonestly.

Each lane has its own entry gates:

- **patient** (2-12h band flips): must be trading at the band's buy target right now, pass a
  regime guard on 6h data that blocks downtrending items (a fat paper margin usually means a
  downtrend, not oscillation), and stay net positive when the band's rules are replayed over
  history with the same 12h stop the live plan uses.
- **active** (15-90 minute flips on items over 1m): fresh two-sided quotes, after-tax margin
  and ROI floors, real flow on both sides, and every call reports its expected loss if the
  spread does not close.
- **time** (recurring UTC windows): selected on older data, then required to stay profitable on
  a newer holdout window it has never seen.
- **probe**: small near-band experiments, capped at 5% of liquid in total.

Survivors compete for free slots by expected realized gp/hour, not paper margin: a fat spread
that fills once a day loses to a thin one that actually turns over. Quantity is capped by the GE
limit and estimated fillability, each slot must clear a profit floor, and the planner sizes
against all of your stated liquid. When gates leave part of it unspent, the plan names the
blocking constraint instead of filling slots with weak trades.

Each call is written as an intent: item, side, quantity, price, strategy, reason, prediction.
Place that exact offer and the plugin fork tags it; later runs grade the call against your real
fills. Five profitable round trips make an item a personal staple, which raises sizing
confidence but never bypasses a gate.

## Setup

Requires Git and `uv`.

```bash
git clone git@github.com:sasoder/evidence-based-flipping.git
cd evidence-based-flipping
uv sync
```

Then ask your agent to set things up — the `setup` skill verifies the plugin wiring and asks for
the two things defaults can't infer: a contact for the OSRS Wiki user-agent, and your `rsn` if
you have more than one RuneLite profile. (Both live in gitignored `config/settings.json`; with a
single profile and no config, everything is auto-detected.)

### RuneLite plugin

You also need the [sasoder/rl-plugin](https://github.com/sasoder/rl-plugin) fork of Flipping
Utilities. The stock plugin does not export current GE slots or consume intent tags, and without
those the planner can't see your open offers or attribute fills to its calls.

```bash
git clone git@github.com:sasoder/rl-plugin.git
```

Build and install it in RuneLite, then enable auto-save (1 minute interval) and "Export current
GE slots". The fork's
[Merch harness integration](https://github.com/sasoder/rl-plugin#merch-harness-integration)
section has a screenshot of both settings and explains the files it writes.

## CLI

The planner is a plain CLI underneath, if you want a plan without the agent:

```bash
uv run python -m merch.plan --cash 50000000 --lanes active --max-new-slots 5 --write-intents --markdown
```

- `--cash <gp>`: liquid GP to size against. Required.
- `--lanes patient,active,time,probe`: enabled lanes, or presets like `balanced` and `conservative`.
- `--max-new-slots <n>`: cap new offers after open-offer triage.
- `--horizon overnight`: drop keyboard-dependent lanes and size for 12h away.
- `--write-intents`: queue exact offer signatures for the plugin fork to tag.

## Runtime data

Your exports and local state stay on disk and out of git:

- `data/incoming/flipping/`, `data/incoming/ge-slots/`
- `state/offer_ages.json`, `state/offer_fills.json`

## Why "Evidence"?

Evidence is my RSN, and the suggestions are evidence-based, so the name was sitting right
there. Here's a week of following the calls on roughly 85m liquid:

<p align="center">
  <img src="images/evidence.png" height="220">
  <img src="images/profit-week.png" height="220">
</p>

About 16m for the week. It would look more impressive if I wasn't poor.

## Tests

```bash
scripts/test.sh
```

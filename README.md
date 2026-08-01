# Evidence-Based OSRS Flipper

**Evidence-Based OSRS Flipper is an OSRS Grand Exchange flipping advisor.** It reads your
Flipping Utilities data, checks OSRS Wiki prices, and gives you exact buy/sell/cancel/reprice
instructions to type into the GE. It never touches the game, you still place every offer
yourself.


<p align="center">
  <img src="images/demo.png" alt="Asking the agent for a flip plan and getting back an action table" width="700">
</p>

Each recommendation records why it was made and what it expects to happen (direction, target, and deadline). Later runs check those calls against what actually filled.

Inspired by Leverage In Action's
["I Tried Using Data Science to Profit in a Video Game Economy"](https://www.youtube.com/watch?v=FhTLApOoWX8).

## Getting started

You need Git, Python 3.11+, and a coding agent unless you want to use the CLI directly.
[uv](https://docs.astral.sh/uv/) is the recommended Python runner, but any activated Python
3.11+ environment works — omit `uv run` from the commands below when using one.

1. **Clone**
  ```bash
   git clone https://github.com/sasoder/evidence-based-osrs-flipper.git
   cd evidence-based-osrs-flipper
  ```
2. **Run the RuneLite plugin fork** —
  [sasoder/rl-plugin](https://github.com/sasoder/rl-plugin). Remove the Plugin Hub version of
  Flipping Utilities, then start the fork with its development runner; a client started normally
  through the RuneLite or Jagex Launcher will not load it. Enable the plugin, one-minute auto-save,
  and "Export current GE slots." The fork README owns the build, launch, and Jagex-account details.
3. **Open the repo in your agent and run `/setup`.** It checks that the
  plugin is exporting data, then asks only for the OSRS Wiki contact, your RSN (if you have more
  than one RuneLite profile), and any subreddits you want the optional research pass to read.
  Answers go into gitignored `config/settings.json`. With one profile and no Reddit sources, no
  config is needed at all.

Then just talk to it: *"50m liquid, what should I buy?"*

## Using it

Typical requests, in plain chat:

- **"50m liquid, what should I do?"** — syncs your exports, checks every open offer (hold, cancel, collect, or reprice), then fills free slots. Offer-only review works the same way with no cash: *"what should I do with my current offers?"*
- **"Are the items I usually flip still good right now?"** — reads your Flipping Utilities history, re-checks those winners against today's prices and the same gates, and only keeps ones that still clear.
- **"Going to bed, 120m."** — overnight mode: sizes positions to a 12-hour window.
- **"Only active flips, max 5 slots."** — preferences go straight to the planner as hard limits.
- **"Anything being talked about that's worth flipping?"** — optional research pass over the OSRS news feed and Reddit. It re-ranks trades that already passed the evidence gates (and is the only way a breaking market gets bought into), but it never invents a trade.

If your message doesn't include the numbers, the agent asks: liquid GP, whether you'll be
around, which strategies, and slot cap. A plan looks like this:


| action | item             | qty | price   | capital   | exp. profit | live lo/hi      | sell target | deadline  | basis                                  |
| ------ | ---------------- | --- | ------- | --------- | ----------- | --------------- | ----------- | --------- | -------------------------------------- |
| buy    | Topaz amulet (u) | 439 | 3,153   | 1,384,167 | 29,852      | 3,153/3,300     | 3,293       | 19:43 UTC | time-of-day, cancel zero-fill after 6h |
| buy    | Granite maul     | 4   | 149,001 | 596,004   | 12,856      | 149,000/156,501 | 156,500     | 21:13 UTC | active, cancel unfilled after 30m      |


Every row includes the latest instant-sell/instant-buy prices (`live lo/hi`), the gp the offer
commits (`capital`), its expected after-tax profit, and a compact basis for the action, so you can
sanity check the call before placing it. The full evidence remains attached to the intent for later
grading.

## How it decides

The LLM is not choosing trades. The planner (`flipper.plan`) does the ranking, sizing, open-offer checks, and formatting. The agent just collects your inputs, runs it once, and presents the results.

Patient buy/sell bands come from percentiles over recent hourly prices (buy near the 35th percentile of instant-sells, sell near the 75th of instant-buys). Live plans post at today's executable prices when those sit close enough to the band, and every margin is after GE tax. Open offers are checked before anything new is suggested: zero-fill buys cancel after 4h, old sells move toward the live bid, and the hard exit is counted even at a loss.

Each strategy has its own checks:


| strategy    | horizon                     | must pass                                                                                                                                                                                                 |
| ----------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **patient** | 2-6h holds, 12h hard exit   | live price near the buy band; 6h regime/trend check; today's buy/sell prices replay profitably over recent history, including forced exits                                                                |
| **active**  | 15-90m, items from 1m up    | fresh two-sided quotes, ROI floor, real flow on both sides; repeatable positive replay; sized so forced-exit downside fits a shared lane risk budget                                                       |
| **time**    | recurring UTC windows       | picked on older data, still profitable on newer holdout data, and today's prices also clear a replay with forced exits                                                                                    |
| **probe**   | small near-band experiments | same replay gate as patient, but fill reachability is unvalidated; capped at 5% of liquid in total                                                                                                        |


Items compete for free slots by expected realized gp/hour from that replayed evidence, not just the live spread. Quantity is capped by GE limit, expected fills, budget, and (for patient/time) per-position downside; active shares one lane-wide downside budget. Each slot must clear a flat 1,000gp profit floor and a capital-return floor. When filters leave liquid unspent, the plan names the binding constraint instead of inventing weak fills.

Each call is written as an intent (item, side, quantity, intended price, strategy, reason,
prediction). The plugin identifies the offer by item, side, and quantity; changing the price does
not prevent tagging. The planner still gives one concrete price so the call is executable and its
outcome can be evaluated. Later runs grade the call against your real fills. Items with a strong
personal FU history can be pulled in as candidates and sized with your fill evidence, but they
still have to pass the same gates.

## CLI

The planner is a plain CLI underneath, if you want a plan without the agent:

```bash
uv run python -m flipper.plan --cash 50000000 --strategies active --max-new-slots 5 --write-intents --markdown
```

- `--cash <gp>`: liquid GP to size against. Required.
- `--strategies patient,active,time,probe`: enabled strategies, or presets like `balanced` and `conservative`.
- `--max-new-slots <n>`: cap new offers after checking current offers.
- `--horizon overnight`: drop keyboard-dependent strategies and size for 12h away.
- `--write-intents`: queue order identities, intended prices, and evidence for the plugin fork.
- `--report-personal-history`: show historical FU items that did not clear today's checks.

## Runtime data

Your exports and local state stay on disk and out of git:
`data/incoming/flipping/`, `data/incoming/ge-slots/`, `state/offer_ages.json`,
`state/offer_fills.json`.

## Why "Evidence"?

Evidence is my RSN, the suggestions are evidence-based, and the rest of the name describes what
it does. A week of following the calls on around 85m liquid returned about 16m, mostly between
Sailing sessions and whenever I got around to the GE, which would look more impressive if I
wasn't poor.

<p align="center">
  <img src="images/profit-week.png" alt="Flipping Utilities weekly profit graph showing about 16m profit" height="220">
  <img src="images/evidence.png" alt="The RSN Evidence in-game" height="220">
</p>

## Tests

```bash
uv run python -m unittest
```

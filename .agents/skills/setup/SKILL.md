---
name: setup
description: One-time setup of the flipping harness for a new player. Use when the user asks to set up, install, onboard, or verify the repo or its RuneLite/Flipping Utilities integration.
---

# Setup

Goal: dependencies installed, RuneLite exports flowing, and a config only if the defaults can't
infer something. Directories, caches, and state files are all created on demand at runtime — do
not pre-create them.

Run all non-interactive setup and verification commands yourself. Do not ask the user to activate
a Python environment, paste Python commands, or inspect command output. For interactive steps,
tell them what to do and ask them to reply when they are finished, then continue the checks yourself.

## Steps

### 1. Dependencies

Require Python 3.11+. Git is preferred, but do not block setup when the user downloaded the repo
as a ZIP. Prefer `uv` when it is already available: run `uv sync` and use `uv run python` for the
commands below. Do not make installing uv a blocker; an activated Python 3.11+ environment has
everything this dependency-free project needs, so use `python` directly. Run the suite with the
selected interpreter:

```bash
uv run python -m unittest
```

### 2. RuneLite data

The harness reads exact prices and terminal history from RuneLite's built-in Grand Exchange plugin.
It must be enabled, but no download, fork, development client, or sideloaded plugin is required.
The Plugin Hub version of Flipping Utilities is optional but strongly recommended: enable its
auto-save and set the interval to one minute so slot changes reach the harness quickly. First
verify the wiring:

```bash
uv run python -m flipper.sync
uv run python -m flipper.runelite status
```

With normal RuneLite open and logged in, status should name the account and report
`slot_source: flipping_utilities` after the next one-minute autosave. `offers: 0` is valid when
every slot is empty. If the snapshot already resolves the intended profile, do not reinstall or
relaunch anything.

Fresh stock Flipping Utilities data is the preferred source for slot presence, quantities, fills,
and timestamps. RuneLite core confirms exact limit prices after its batched config write. A matched
intent supplies a clearly provisional price until then. Without Flipping Utilities, core-only mode
still works but current changes can lag by about five minutes.

Diagnose a failure from the first broken link:

| observation | check next |
|---|---|
| no `data/incoming/runelite/profiles.json` | `flipper.sync` output and resolved `RUNELITE_HOME` |
| enabled GE plugin sees an offer but disk does not | RuneLite batches profile writes; allow up to five minutes for its next flush |
| `slot_source` remains `runelite` | enable stock FU auto-save, set its interval to one minute, and run status after its next autosave |
| snapshot has no intended profile | normal RuneLite has been launched and the account logged in at least once |
| reader reports ambiguity | RSN, profile type, RuneLite's selected profile log, then `runelite_profile` config |
| reader prints `[]` | valid when all GE slots are empty |
| unrelated tool fails | diagnose its actual error; do not add machine-specific config or instructions |

Never select an arbitrary duplicate profile. Prefer RuneLite's own selected-profile log; ask once
and persist `runelite_profile` only when duplicates remain genuinely ambiguous.
RuneLite's default data directory is `~/.runelite` on every supported desktop platform
(`%USERPROFILE%\.runelite` on Windows). Use `RUNELITE_HOME` only for a genuinely non-default
installation.

### 3. Config — only what defaults can't infer

Ask in one round (a single structured multi-question prompt if the engine supports one):

- **Wiki contact**: an email/Discord/RSN for the OSRS Wiki API user-agent. The Wiki asks that
  clients be identifiable; the shipped placeholder works but is impolite to leave.
- **RSN**: ask only if `data/incoming` shows zero or multiple RuneLite account names — a single
  profile is auto-detected and needs no config.
- **Research subreddits** (multi-select): which subreddits the optional research overlay reads.
  Default to no subreddits selected. Offer exactly these options: `2007scape` (recommended),
  `OSRSflipping` (recommended), `GrandExchange` — plus the engine's built-in "Other" for custom
  subs. Do not invent additional options (no "None"/"Skip" entry: an empty selection means no
  Reddit sources). Write `research.subreddit` only when the user selects one or more subreddits.

Write the answers to gitignored `config/settings.json` (shape in `config/settings.example.json`,
which also serves as the fallback when no settings.json exists). Format the user-agent as
`osrs-ge-merchanting-harness/0.1 (contact: <answer>)`. If the user skips every question, write
nothing — the defaults work.

### 4. Done

Suggest a first run: "I have 50m spendable outside the GE; what should I buy?" (the `flip` skill
takes it from there).

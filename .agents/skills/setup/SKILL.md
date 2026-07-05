---
name: setup
description: One-time setup of the flipping harness for a new player. Use when the user asks to set up, install, onboard, or verify the repo or its RuneLite/Flipping Utilities integration.
---

# Setup

Goal: dependencies installed, RuneLite exports flowing, and a config only if the defaults can't
infer something. Directories, caches, and state files are all created on demand at runtime — do
not pre-create them.

## Steps

### 1. Dependencies

Verify `uv` is installed (if not, point the user to
https://docs.astral.sh/uv/getting-started/installation/), then run `uv sync` and:

```bash
uv run python -m unittest discover -s tests -v
```

### 2. RuneLite plugin

The harness requires the [sasoder/rl-plugin](https://github.com/sasoder/rl-plugin) fork of
Flipping Utilities with autosave (1-minute interval) and "Export current GE slots" enabled —
the README's "RuneLite plugin" section covers building and installing it. Verify the wiring:

```bash
scripts/runelite-sync.sh
uv run python -m flipper.runelite offers
```

With the game open, offers (or `[]` on empty slots) should print. If the export is missing or
stale, that is the blocker to fix — walk the user through the plugin settings; never write
config to paper over missing exports.

### 3. Config — only what defaults can't infer

Ask in one round (a single structured multi-question prompt if the engine supports one):

- **Wiki contact**: an email/Discord/RSN for the OSRS Wiki API user-agent. The Wiki asks that
  clients be identifiable; the shipped placeholder works but is impolite to leave.
- **RSN**: ask only if `data/incoming` shows zero or multiple Flipping Utilities profiles — a
  single profile is auto-detected and needs no config.
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

Suggest a first run: "50m liquid, what should I buy?" (the `flip` skill takes it from there).

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

Require Git and Python 3.11+. Prefer `uv` when it is already available: run `uv sync` and use
`uv run python` for the commands below. Do not make installing uv a blocker; an activated Python
3.11+ environment has everything this dependency-free project needs, so use `python` directly.
Run the suite with the selected interpreter:

```bash
uv run python -m unittest
```

### 2. RuneLite plugin

The harness requires the [sasoder/rl-plugin](https://github.com/sasoder/rl-plugin) fork of
Flipping Utilities with autosave (1-minute interval) and "Export current GE slots" enabled —
the fork README owns its build, launch, and Jagex-account instructions. First verify the wiring:

```bash
uv run python -m flipper.sync
uv run python -m flipper.runelite offers
```

With the game open, offers (or `[]` when every slot is empty) should print. If fresh exports
already exist, do not reinstall or relaunch anything.

If the fork is not installed, offer to guide and perform the deterministic setup steps: check Git
and a suitable Java runtime, clone the fork into a sibling or user-selected directory, and start
its Gradle `runPlugin` task. Do not use `~/.runelite/sideloaded-plugins`: the supported path is the
development client started by the fork's runner, not a jar loaded by a normal RuneLite or Jagex
Launcher client. A Jagex account needs the one-time credential handoff described by the fork;
authentication and enabling the plugin remain interactive user steps.

Remove the Plugin Hub copy of Flipping Utilities before starting the fork. In the development
client, ensure the fork itself is enabled, then enable one-minute auto-save and "Export current GE
slots." Diagnose a failure from the first broken link:

| observation | check next |
|---|---|
| no source `current-slots/<rsn>.json` | development-client launch, duplicate stock plugin, plugin enabled, export enabled, logged in |
| source fresh but repo mirror absent or stale | `flipper.sync` output and resolved `RUNELITE_HOME` |
| repo mirror fresh but reader errors | RSN/config ambiguity |
| reader prints `[]` | valid when all GE slots are empty |
| unrelated tool fails | diagnose its actual error; do not add machine-specific config or instructions |

Missing or stale exports are the blocker to fix; never write config to paper over them.

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

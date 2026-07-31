# Agent Instructions

Evidence-Based OSRS Flipper is an OSRS Grand Exchange flipping advisor. A deterministic planner
(`flipper.plan`) picks the trades; the agent's job is to collect inputs, run the planner, and
present its output as exact GE instructions the user executes manually in-game. Nothing in this
repo can touch the game.

For a flip planning session, read and follow `.agents/skills/flip/SKILL.md` — the single source
of truth for the workflow, planner commands, sizing rules, and presentation contract. Do not
duplicate it elsewhere: `.agents/skills/` is the canonical skill location, and tool-specific
copies (e.g. `.claude/skills/`) are thin pointers back to it.

Python managed with `uv`. Run tests with:

```bash
uv run python -m unittest
```

For planner optimization, `evaluation/` is the frozen oracle. Do not edit its contract, runner,
fixtures, comparison logic, or tests in the same change as planner behavior. Run
`uv run python -m evaluation.runner --output evaluation/results/v2-current.json`, then compare
against `evaluation/baselines/v2-main.json`. Evaluator changes require separate human review and
baseline regeneration.

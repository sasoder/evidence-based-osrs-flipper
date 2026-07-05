# Research overlay playbook

Optional slow path for `/flip` (or any agent harness driving `flipper.plan`). Run it only when the
user explicitly asks for research/catalysts/news, or the request is clearly about a current
event. Otherwise skip it; it usually adds latency and often produces no overlay.

## Procedure

First run the normal planner JSON and the research brief:

```bash
uv run python -m flipper.plan --cash <liquid_gp>
uv run python -m flipper.research brief
```

Read only two things: the `research brief` digest, and the candidate *names/ids* from the
draft's `buys` + `skipped` lists. Produce a small overlay that maps catalysts to items.

Rules the planner enforces (not the agent): **boost only re-ranks gate survivors; avoid vetoes a
survivor. Research never bypasses the strategy gate.** Translate findings into positioning, not
trivia. Use a subagent only to *interpret* the digest or chase un-wired sources (X/Twitter,
blogs), returning a ≤10-line findings→implication summary — never to fetch raw pages on the main
thread. `research brief` never fails silently — cite any concrete `error` (e.g. `HTTP 403`),
never "web checks unavailable".

If research surfaces nothing actionable, stop and run the normal one-shot final command without
an overlay. If there is an actionable overlay, run the final deterministic plan once with it:

```bash
uv run python -m flipper.plan --cash <liquid_gp> \
    --overlay overlay.json --write-intents --markdown
```

For overnight/away-from-keyboard requests, include the same `--horizon overnight --seed-limit 0
--time-seed-limit 0` flags on this final command.

Present the planner markdown unchanged. Include the planner's deployment utilization.

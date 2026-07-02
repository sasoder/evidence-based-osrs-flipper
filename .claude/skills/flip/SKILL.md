---
name: flip
description: On-demand OSRS GE merchanting advice. Invoke to produce an exact buy/sell/reprice table from Flipping Utilities exports and merch.plan.
---

# /flip — on-demand merchanting advice

Follow the **Runtime workflow** section of the repo-root `AGENTS.md` exactly; that file is the
engine-agnostic contract and this skill only adds Claude Code specifics:

- Step 0: collect the missing inputs with a **single `AskUserQuestion` call** (never plain-text
  questions). Radio for attendance and slots, multi-select for the four base lanes, gp options
  `10m` / `50m` / `100m` / `250m` with exact amounts via Other.
- Step 3 research: use a subagent only to *interpret* the research digest or chase un-wired
  sources, returning a ≤10-line findings→implication summary — never to fetch raw pages on the
  main thread.

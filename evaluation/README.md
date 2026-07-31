# Frozen Evaluator V2

Run the frozen evaluator:

```bash
uv run python -m evaluation.runner --output evaluation/results/v2-current.json
```

Full evaluator results belong under the gitignored `evaluation/results/` directory. When
regenerating the `origin/main` reproducibility baseline, run the current evaluator against the
pinned planner worktree and write both artifacts in one pass:

```bash
uv run python -m evaluation.characterize \
  --planner-root /path/to/origin-main-worktree \
  --planner-revision 0adfe38267d0040974b8aa38ec92212c54a7766e \
  --output evaluation/results/v2-main-full.json \
  --baseline-output evaluation/baselines/v2-main.json
```

The checked-in baseline is a deterministic projection of the full result. It retains comparison
identities, fixture hashes, coverage identity, contract notes, cohort gates, and compact summary
metrics; per-case output remains only in the ignored full result.

Use `--enforce` for a planner candidate. It succeeds only when the observed and synthetic
lane-local cohort gates both pass. It does not assess a mixed-lane whole-planner portfolio.

Compare a candidate with the V2 reproducibility baseline:

```bash
uv run python -m evaluation.compare \
  evaluation/baselines/v2-main.json evaluation/results/v2-current.json
```

The baseline supplies oracle and corpus identity plus characterization deltas. It does not relax
the candidate gate. The selected challenger-materiality threshold is frozen at 0.5bp.

Regenerate the deterministic synthetic fixture:

```bash
uv run python -m evaluation.generate_fixtures
```

Import an immutable observed archive only through the integrity- and coverage-gated importer:

```bash
uv run python -m evaluation.import_cache_fixture \
  --archive-dir /path/to/2026-07-29T10:38:13Z \
  --date 2026-07-29 \
  --output evaluation/fixtures/observed_2026-07-29.json.gz
```

Read [SPEC.md](SPEC.md) before changing planner behavior. The July 27/29 outcomes become
development regression evidence once tuning starts; the spec defines the required later
fresh-snapshot validation. Evaluator changes require separate human review and baseline
regeneration. The renamed `v1-main.json` baseline and phase-one report are exploratory records, not
active acceptance inputs.

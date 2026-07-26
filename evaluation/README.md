# Planner evaluator

Run the frozen corpus from the repository root:

```bash
uv run python -m evaluation.runner --output evaluation/results/current.json
```

Add `--enforce` when evaluating an optimization candidate. Baselines intentionally record existing
failures and therefore do not use enforcement.

Compare an iteration with the baseline during the repair phase:

```bash
uv run python -m evaluation.compare \
  evaluation/baselines/main.json evaluation/results/current.json --mode repair
```

After the violation count reaches zero, use `--mode strict`.

Regenerate the permanent synthetic corpus:

```bash
uv run python -m evaluation.generate_fixtures
```

The frozen July 11 and July 22 corpus retains every eligible item. Do not regenerate it from a
current local cache: cache entries are overwritten by API URL. Add later dates as separate
immutable fixtures:

```bash
uv run python -m evaluation.import_cache_fixture \
  --cache-dir /path/to/source-cache \
  --dates 2026-07-26 \
  --output evaluation/fixtures/real_market_2026-07-26.json.gz
```

For a future corpus that is too large to check in, pass `--cohort-size N` to use the deterministic
stratified cohort policy described in the specification.

Read [SPEC.md](SPEC.md) before changing planner behavior. Evaluator changes require separate review
and invalidate all stored baselines.

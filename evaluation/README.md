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

The observed multi-date corpus was imported from local Wiki cache envelopes with every eligible
item retained:

```bash
uv run python -m evaluation.import_cache_fixture
```

For a future corpus that is too large to check in, pass `--cohort-size N` to use the deterministic
stratified cohort policy described in the specification.

Read [SPEC.md](SPEC.md) before changing planner behavior. Evaluator changes require separate review
and invalidate all stored baselines.

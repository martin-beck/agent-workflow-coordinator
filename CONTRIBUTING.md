# Contributing

Open an issue describing the invariant, compatibility impact and failure/recovery behavior before a
large change. Keep patches focused and include tests and documentation. Core protocol changes need
a formal-model update or a written explanation of why the abstraction is unchanged. Backend changes
must pass shared lifecycle behavior plus real independent-process, contention, crash, binding,
migration-equivalence and deterministic-projection tests.

Run:

```sh
uv sync --locked --only-group quality
uv run ruff format --check tools tests
uv run ruff check --no-fix tools tests
uv run mypy tools tests
uv run coverage run --branch -m unittest discover -s tests -p 'test_*.py'
uv run coverage report --fail-under=95
formal/handoffctl/verify.sh
```

Use signed commits with a matching `Signed-off-by` trailer. By contributing, you certify the
Developer Certificate of Origin 1.1.

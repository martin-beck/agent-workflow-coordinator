# Contributing

Open an issue describing the invariant, compatibility impact and failure/recovery behavior before a
large change. Keep patches focused and include tests and documentation. Core protocol changes need
a formal-model update or a written explanation of why the abstraction is unchanged. Backend changes
must pass shared lifecycle behavior plus real independent-process, contention, crash, binding,
migration-equivalence and deterministic-projection tests.

Run:

```sh
uv sync --locked --only-group quality
uv run python tools/check_source_headers.py
uv run ruff format --check tools tests
uv run ruff check --no-fix tools tests
uv run mypy tools tests
uv run coverage run --branch -m unittest discover -s tests -p 'test_*.py'
uv run coverage report --fail-under=95
formal/handoffctl/verify.sh
```

The source-header check covers tracked Python and shell sources, TLA+ modules, and the extensionless
`tools/handoffctl` launcher. It intentionally excludes documentation, JSON schemas and examples,
TLC configuration, TOML project metadata, YAML workflows, and lock files because those are
documentation, data, or configuration rather than source code. A shebang may precede the header;
TLA+ modules keep their required MODULE declaration first and place the header immediately after it.
The exact Huawei copyright and SPDX lines must form one adjacent pair at that required location;
standalone matching lines elsewhere do not count as duplicate headers.

Use signed commits with a matching `Signed-off-by` trailer. By contributing, you certify the
Developer Certificate of Origin 1.1.

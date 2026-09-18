# Quality gates

The coordinator retains its native verification gates as authoritative. Agent
Workflow Quality (AWQ) v0.35.0 adds a pinned, portable PR layer; it does not
replace Python tests, branch coverage, Ruff, mypy, Lizard, JSON Schema checks,
source-header checks, DCO validation, or the TLA+ models.

The tracked AWQ policy and lock select these matching profiles:

- `core`, `docs`, `github-actions`, `privacy`, `python`, `schemas`, `shell`,
  `supply-chain`, and `formal-evidence`.

The lock pins AWQ commit
`d3d46a5f540fb03f65f30dd4c2af325f6f018a6e` (release `v0.35.0`). CI installs
that exact source revision from the locked quality group, runs onboarding, and
executes the AWQ PR tier before the native verification job. The repository
owned `quality/workflow-trust.json` supplies the workflow event and runner
trust declarations required by AWQ-GHA-002. Trusted push, dispatch, and weekly
formal execution is isolated in `.github/workflows/formal.yml` on the approved
self-hosted runner; pull requests use the disposable portable formal tier in
`verify.yml`.

The native-to-shared boundary is intentional:

| AWQ area | Coordinator-owned authoritative gate |
| --- | --- |
| Python syntax and portable text | Ruff, mypy, tests, and branch coverage |
| Shell entry points | launcher mode and source-header tests |
| JSON and classified formats | JSON Schema and example validation |
| Formal-evidence claims | TLC models and formal contract tests |
| GitHub Actions and privacy | reviewed workflow permissions, immutable actions, and public-safe source review |
| Supply-chain policy | coordinator vendor manifest and locked quality policy |

AWQ adapter execution remains opt-in and does not acquire tools at runtime.
The coordinator-specific `terminology` profile is deferred until a reviewed
terminology registry and negative fixtures are added.

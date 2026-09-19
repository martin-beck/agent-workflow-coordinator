# Quality gates

The coordinator retains its native verification gates as authoritative. Agent
Workflow Quality (AWQ) v0.32.0 adds a pinned, portable PR layer; it does not
replace Python tests, branch coverage, Ruff, mypy, Lizard, JSON Schema checks,
source-header checks, DCO validation, or the TLA+ models.

The tracked AWQ policy and lock select these matching profiles:

- `core`, `docs`, `github-actions`, `privacy`, `python`, `schemas`, `shell`,
  `supply-chain`, and `formal-evidence`.

The lock pins AWQ commit
`f1c40859e9d10cacbc79100ed136c38bce48cd39` (release `v0.32.0`). CI checks out
that exact source revision, installs its locked environment, runs onboarding,
and executes the AWQ PR tier before the native verification job.

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
The coordinator-owned `quality/terminology.json` registry supplies the terms;
AWQ performs bounded offline lexical checking in the declared documentation and
example scopes. This is a terminology-contract gate, not semantic or policy
proof. Native coordinator gates remain authoritative.

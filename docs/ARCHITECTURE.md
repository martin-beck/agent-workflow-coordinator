# Architecture

`tools/handoffctl.py` is the canonical runtime and `tools/handoffctl` is its portable launcher.
The runtime owns parsing, validation, transitions, locking, atomic replacement, bounded subprocess
calls, commit/replication, project binding and CLI dispatch. `status_renderer.py` is deterministic
presentation code and has no mutation authority. `CURRENT.md` is the compact actionable
projection and includes only `in_progress`, `open`, `blocked`, `planned`, and `future` tasks;
terminal history remains available in the full `STATUS.md` projection and task records.

One project-bound backend is authoritative. New projects use `.runtime/coordinator.sqlite3` in WAL
mode. Its strict relational schema stores binding metadata, tasks, dependency edges, uniqueness
constraints, revisions, append-only events, command results, checkpoints and migrations. Short
`BEGIN IMMEDIATE` transactions and conditional revision updates are the SQLite linearization point.
Task Markdown and status JSON/Markdown are disposable, byte-stable projections.
Session and checkpoint records are authoritative in the selected backend: Git stores bounded JSONL
journals, while SQLite stores equivalent strict JSON records and regenerates those journals as
projections. Task specifications and hierarchy edges remain part of each task record in both
backends. Directives remain a Git-authority journal and their command path stays fail-closed under
SQLite until a dedicated backend is reviewed.
Blocked provenance is revision-local: `pause` atomically creates the sole current-revision paused
session, while `release --status blocked` creates no session. `resume` accepts only the former and
`unblock` accepts only the latter; malformed or ambiguous current-revision evidence fails closed.
Gate metadata supports the generic ordered `role` -> `spec` -> `decision` stages for new task
specifications. Existing interaction-gate records retain the legacy
`intake` -> `discussion` -> `formal_spec_review` -> `reconciliation` sequence; stage parsing and
revision checks fail closed for unknown or skipped stages.

The opt-in Git backend retains the original model: task JSON front matter and Markdown bodies are
authority, and the repository-common lock serializes linked worktrees through one inode. A tracked
`coordinator.backend.json` permanently selects the backend. Its absence has one compatibility
meaning only: an existing installation remains Git-backed.

Git lifecycle mutation admission is intentionally narrower than the whole-repository `doctor`
predicate. Under the common lock it validates the resulting target task, complete dependency graph,
generated views, and global active task, owner, branch, and worktree uniqueness. It also compares
mutation-owned files before and after the transition, rejecting newly introduced privacy or size
findings. Existing unrelated findings remain visible to `doctor`; expiry, privacy, and size
findings do not block otherwise safe lifecycle transitions.

Dependency admission is fail-closed: `done` satisfies a dependency directly. A `superseded` task
requires an explicit `superseded_by` successor chain ending at an existing `done` task; malformed,
missing, cyclic, or unfinished chains never satisfy admission.

Project identity has three layers: a tracked profile, the permanent UUID/repository binding, and
the backend selector. SQLite repeats the identity inside the database. Runtime paths and database
identity must match that binding before normal command execution.

The vendor tool normally copies an allowlist from one exact tagged upstream commit. Its explicit
development-only path accepts an untagged head only when the clean source is at the requested full
commit, and records that commit, its Git tree, and a `development` classification. A downstream
lock manifest records each source/destination and SHA-256. Release manifests retain their existing
tag-bound schema and behavior. The downstream verifier is itself part of the manifest. Project
profile and binding files are outside the vendor allowlist.

TLA+ models are AI-generated best-effort design guidance for the implementation, not required
refinement proofs of the Python interpreter,
operating system, Git, GitHub, or filesystem. Implementation tests connect the abstract contracts to
real SQLite connections and processes, flock contention for Git/projections, atomic files,
subprocess failures and project profiles.

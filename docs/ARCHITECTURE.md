# Architecture

`tools/handoffctl.py` is the canonical runtime and `tools/handoffctl` is its portable launcher.
The runtime owns parsing, validation, transitions, locking, atomic replacement, bounded subprocess
calls, commit/replication, project binding and CLI dispatch. `status_renderer.py` is deterministic
presentation code and has no mutation authority.

One project-bound backend is authoritative. New projects use `.runtime/coordinator.sqlite3` in WAL
mode. Its strict relational schema stores binding metadata, tasks, dependency edges, uniqueness
constraints, revisions, append-only events, command results, checkpoints and migrations. Short
`BEGIN IMMEDIATE` transactions and conditional revision updates are the SQLite linearization point.
Task Markdown and status JSON/Markdown are disposable, byte-stable projections.

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

Project identity has three layers: a tracked profile, the permanent UUID/repository binding, and
the backend selector. SQLite repeats the identity inside the database. Runtime paths and database
identity must match that binding before normal command execution.

The vendor tool copies an allowlist from one exact tagged upstream commit. A downstream lock
manifest records each source/destination and SHA-256. The downstream verifier is itself part of the
manifest. Project profile and binding files are outside the vendor allowlist.

TLA+ models are refinement targets for the implementation, not proofs of the Python interpreter,
operating system, Git, GitHub, or filesystem. Implementation tests connect the abstract contracts to
real SQLite connections and processes, flock contention for Git/projections, atomic files,
subprocess failures and project profiles.

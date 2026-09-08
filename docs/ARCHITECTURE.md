# Architecture

`tools/handoffctl.py` is the canonical runtime and `tools/handoffctl` is its portable launcher.
The runtime owns parsing, validation, transitions, locking, atomic replacement, bounded subprocess
calls, commit/replication, project binding and CLI dispatch. `status_renderer.py` is deterministic
presentation code and has no mutation authority.

The state repository is the database. Task JSON front matter is authoritative; Markdown bodies hold
concise evidence. Generated Markdown is a projection. Machine configuration and the command-result
journal live in ignored `.runtime`; the shared lock lives below Git's common directory so linked
worktrees serialize through one inode. None is portable authority.

Project identity has two layers: a tracked profile for presentation/policy and a tracked binding for
the permanent UUID and repository pair. Runtime paths must resolve to repositories matching that
binding. Checks occur before normal command execution.

The vendor tool copies an allowlist from one exact tagged upstream commit. A downstream lock
manifest records each source/destination and SHA-256. The downstream verifier is itself part of the
manifest. Project profile and binding files are outside the vendor allowlist.

TLA+ models are refinement targets for the implementation, not proofs of the Python interpreter,
operating system, Git, GitHub, or filesystem. Implementation tests connect the abstract contracts to
real flock contention, atomic files, subprocess failures and project profiles.

# Agent Workflow Coordinator

Agent Workflow Coordinator is a small, self-contained coordination database and CLI for people
and autonomous coding agents sharing a project. New projects use an embedded SQLite 3 database in
WAL mode. Its `handoffctl` command transactionally serializes task claims and updates, fences stale
revisions, maintains leases and generated views, records bounded commands without retaining
sensitive output, and can optionally publish deterministic projections through Git/GitHub.

The project is extracted from the coordinator used by
[Agent Relay State](https://github.com/martin-beck/agent-relay-state) and
[Agent Systems Benchmark State](https://github.com/martin-beck/agent-systems-benchmark-state).

## Why it is separate

One canonical implementation, contract-test suite, and formal model prevents bug-fix drift.
Projects consume a tagged, checksummed vendor snapshot, so coordination remains available offline
and does not depend on a package registry, network fetch, submodule, or upstream checkout.

## Safety properties

- SQLite WAL is the default authority for new projects: foreign keys, database constraints,
  `BEGIN IMMEDIATE`, exact-revision conditional updates, a 10-second busy deadline and
  `synchronous=FULL` provide the local multi-process transaction boundary.
- The existing Markdown/Git backend remains supported with `init --backend git`. Existing projects
  without a backend selector remain Git-backed and are never silently migrated.
- Every accepted mutation is serialized by one repository-common POSIX `flock(2)`, including
  processes launched from different worktrees of the same local clone.
- Exact task revisions reject stale concurrent writers.
- One owner can hold one active task; a task, branch and worktree have one active owner.
- Pre-commit failures restore task and generated-view files.
- Lock, Git/GitHub and wrapped-command waits are bounded and classified.
- Wrapped commands require runtime configuration before execution and fsync a privacy-safe result
  journal before task recording or live reconciliation.
- Keep UUID privacy checks active except in required identity and contract files.
- Clean behind replicas fast-forward before writes; true divergence remains fail-closed.
- A durable local commit is retained if later replication fails.
- Initialization permanently binds an installation to one state repository and one product
  repository. Calls from another project fail before normal coordinator work.
- TLA+/TLC checks bounded transition, lock, safety, liveness, and project-binding abstractions.

Read [the formal proof boundary](formal/handoffctl/README.md): these guarantees assume cooperating
processes on a local filesystem and do not cover malicious source edits, direct writers, unreliable
NFS locking, arbitrary command correctness, kernel/storage failure, or power loss.

## Adopt it in a project

Prerequisites for normal local SQLite coordination are Python 3.12+, Git, and a local filesystem
supporting SQLite WAL locking/shared memory. No database daemon, separate `sqlite3` executable,
Python package, GitHub account, GitHub CLI, or network access is required. GitHub CLI and configured
Git signing are needed only when live GitHub views or Git publication are enabled. The Git backend
also requires a local filesystem with POSIX `flock(2)` semantics.

1. Check out an exact release tag.
2. Vendor it into the state repository:

   ```sh
   python /path/to/agent-workflow-coordinator/tools/vendor.py sync \
     --source /path/to/agent-workflow-coordinator \
     --target /path/to/project-state \
     --version v0.3.3
   ```

3. From the state repository root, initialize exactly once:

   ```sh
   tools/handoffctl init \
     --state-repository OWNER/STATE_REPOSITORY \
     --product-repository OWNER/PRODUCT_REPOSITORY \
     --project-name example-project \
     --project-title "Example Project" \
     --status-view \
     --commit-signoff
   ```

   SQLite WAL is the default. Use `--backend git` for the existing Markdown/Git authority.
   Existing initialized repositories missing `coordinator.backend.json` are treated as legacy Git
   projects. They do not migrate during an upgrade.

4. Create the ignored `.runtime/config.json`, task schema, initial tasks, and project development
   policy as described in [Integration](docs/PROJECT_GUIDE.md).
5. Verify the pin and project:

   ```sh
   python tools/handoffctl_vendor.py verify --target .
   tools/handoffctl doctor
   tools/handoffctl reconcile
   ```

There is intentionally no rebind operation. A second product needs a separately initialized state
repository.

## Use it

```sh
tools/handoffctl snapshot
tools/handoffctl claim AR-0001 --owner worker-unique --lease-minutes 120
tools/handoffctl run --owner worker-unique AR-0001 -- command arg
tools/handoffctl update AR-0001 --owner worker-unique --expected-revision 2 \
  --note "Verified result and next action"
tools/handoffctl release AR-0001 --owner worker-unique --status done \
  --note "Integrated and verified"
tools/handoffctl reconcile --commit --push
tools/handoffctl doctor --live
```

Run the tool from the bound state repository or configured product checkout. The complete
human/agent loop and recovery rules are in [Operations](docs/OPERATIONS.md) and the compact
[Agent guide](docs/AGENT_GUIDE.md).

## Extend it

Project-specific fields, validation and process rules stay downstream. Never patch vendored files.
Changes to transitions, locks, binding, core configuration, or generated-view hooks belong upstream
with contract tests and, where applicable, formal model updates. See
[Extending](docs/EXTENDING.md).

## Repository map

- `tools/handoffctl.py`: canonical vendored runtime.
- `tools/sqlite_storage.py`: embedded WAL schema and transactional backend.
- `tools/status_renderer.py`: deterministic optional portfolio renderer.
- `tools/vendor.py`: tagged-release sync and offline digest verifier.
- `tools/check_source_headers.py`: exact Huawei/MIT source-header verifier.
- `formal/handoffctl/`: TLA+ specifications and pinned TLC runner.
- `tests/`: fault, race, recovery, binding and vendor tests.
- `schema/`: tracked configuration contracts.
- `examples/project/`: safe initialization examples.
- `docs/PROJECT_GUIDE.md`: offline guide included in every vendor snapshot.

Licensed under MIT.

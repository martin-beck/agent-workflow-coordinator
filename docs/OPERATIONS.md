# Operating handoffctl

## Commands

- `init`: one-time permanent state/product binding.
- `snapshot`: validate and print the complete current queue plus live observations.
- `claim`: serialize ownership of one dependency-ready open task.
- `heartbeat`: renew the current owner's lease.
- `update`: exact-revision update of an actively owned task.
- `release`: clear ownership and move to a non-active status.
- `promote`: exact-revision `planned -> open` after dependencies complete.
- `resume`: exact-revision `blocked -> open` after external resolution is confirmed.
- `recover-expired`: exact-revision recovery of an in-progress task only after its UTC lease
  deadline has passed.
- `run`: execute a bounded command outside the lock, then record its classified result.
- `reconcile`: refresh live observations and optionally commit/fast-forward-push them.
- `render-status`: render/check the optional complete status view.
- `doctor`: validate structure; `--live` also checks Git/GitHub observations.
- `migrate --to sqlite|git`: explicitly switch authority after an equivalence-checked export/import.

## Backend selection

`init` defaults to SQLite WAL. `init --backend git` selects the original Git/Markdown authority.
The tracked `coordinator.backend.json` is project-bound and must be committed with initialization.
An older project without that file is always treated as Git-backed; installing a newer handoffctl
does not create a database or change its authority. GitHub is disabled unless machine-local runtime
configuration enables observation/publication.

## Concurrency and timeouts

SQLite projects use one independent connection per process, WAL, foreign keys, a 10-second busy
timeout and `synchronous=FULL`. Writers begin with `BEGIN IMMEDIATE`; the conditional
`UPDATE ... WHERE revision=?` and transaction commit are the linearization point. Database partial
unique indexes enforce one active owner, branch and worktree. Readers do not block a WAL writer.
Known network filesystem types fail closed because WAL shared memory is a same-host local-filesystem
contract. `SQLITE_BUSY_TIMEOUT`, `SQLITE_CORRUPT`, `SQLITE_BINDING_MISMATCH` and
`SQLITE_COMMITTED_EXPORT_FAILED` are stable operator classifications.

Mutations take an exclusive lock stored below Git's common directory, so all worktrees of one local
clone serialize through the same file. Snapshots and status checks take a shared lock. The default
lock deadline is 10 seconds, the internal Git/GitHub deadline is 30 seconds, and the wrapped command
deadline is 1800 seconds. On timeout, inspect the holder, process, commit and ref before retrying.

Commands run outside the coordinator lock so long work cannot block heartbeats. Runtime
configuration and the live claim are checked before execution. The privacy-safe
task/owner/argv-digest/exit record is fsynced to `.runtime/command-results.jsonl` immediately after
execution and before any fallible task, Git, GitHub or reconciliation work. The task update is
committed before live reconciliation. The recorded digest covers argv, not output or environment.

## Durable failure semantics

For SQLite, a committed transaction is never rolled back because projection, Git, GitHub or network
publication failed. If `SQLITE_COMMITTED_EXPORT_FAILED` is reported, inspect the database/task
revision and run `reconcile`; do not repeat an external command. Command results are committed in a
separate transaction before task mutation and optional reconciliation.

Before a write, a configured main replica fetches `origin/main`. A clean behind checkout is
fast-forwarded; a dirty behind checkout or true divergence fails with a classified error. After a
successful command, inspect its journal and task record first; retry only reconciliation or
replication. Never blindly repeat an external command after an interrupted `run`.

A persistent dirty-behind or divergent result writes `.runtime/replica-blocked.json`.
A periodic service should include a matching systemd condition so it stops retrying until an
operator has inspected and manually reconciled the refs:

```ini
ConditionPathExists=!/absolute/state-checkout/.runtime/replica-blocked.json
```

A successful manual `reconcile --commit --push` clears the marker. `doctor` reports it as an error.
## Binding failures

- Profile mismatch: profile and binding UUIDs differ.
- State mismatch: tool location, Git root or origin is not the initialized state repository.
- Product mismatch: runtime product identity or checkout origin differs from the binding.
- Caller mismatch: current directory is outside the bound state/product checkout.

Do not edit identifiers to silence these failures. Return to the correct checkout. A legitimately
different repository must receive a fresh vendor snapshot and one-time initialization.

## Backup and recovery

For SQLite projects, `.runtime/coordinator.sqlite3` and its live `-wal`/`-shm` companions are
authoritative. Back up with Python's standard-library online SQLite backup API or after a clean
checkpoint; do not copy only the main file while writers are active. Run `doctor` (which includes
`PRAGMA integrity_check`) after restore. Keep the project binding and backend selector with backups.
For Git projects, Git history remains durable authority and is backed up normally.

To migrate a valid existing Git project, commit or preserve its current state and run:

```sh
tools/handoffctl migrate --to sqlite
tools/handoffctl doctor
```

The complete database is installed before the backend selector changes. An interruption before the
selector switch leaves Git authoritative. Task IDs, revisions, dependency edges, claims, evidence
bodies and source commit checkpoint are equivalence-checked. Until migration is accepted, original
files remain usable; `migrate --to git` explicitly exports current SQLite state and switches
authority back. Never edit the selector to perform migration.

After process or host failure:

1. inspect active processes and lock holders;
2. inspect task revision/lease, working tree, index and signed commits;
3. fetch without forcing and compare local/remote ancestry;
4. verify the vendor manifest and binding;
5. reconcile without commit, review, then commit/push only the missing state;
6. run static and live doctors.

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

## Concurrency and timeouts

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

Git history is the durable state database. Back up remote refs and repository storage normally.
The ignored runtime directory contains only lock/config/reconciliation observations and can be
recreated, but protect its local configuration permissions. After process or host failure:

1. inspect active processes and lock holders;
2. inspect task revision/lease, working tree, index and signed commits;
3. fetch without forcing and compare local/remote ancestry;
4. verify the vendor manifest and binding;
5. reconcile without commit, review, then commit/push only the missing state;
6. run static and live doctors.

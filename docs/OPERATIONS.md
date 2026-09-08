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
- `run`: execute a bounded command outside the lock, then record its classified result.
- `reconcile`: refresh live observations and optionally commit/fast-forward-push them.
- `render-status`: render/check the optional complete status view.
- `doctor`: validate structure; `--live` also checks Git/GitHub observations.

## Concurrency and timeouts

Mutations take an exclusive lock. Snapshots and status checks take a shared lock. The default lock
deadline is 10 seconds, internal Git/GitHub deadline 30 seconds, and wrapped command deadline 1800
seconds. A timeout is evidence: inspect the holder/process/commit/ref before retrying.

Commands run outside the coordinator lock so long work cannot block heartbeats. Before execution
and again while recording the result, ownership and lease validity are checked. The recorded digest
covers argv, not raw output, prompts or environment.

## Durable failure semantics

Before a commit, detected failure restores every touched task and generated view. After a successful
local commit, later push failure does not roll it back. Inspect and reconcile that commit, then retry
only replication. Never blindly repeat an external command after an interrupted `run`.

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

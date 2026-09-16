# Upgrade runbook: v0.3.8 to v0.3.9

This runbook is generated from a validated release contract. It describes a
bounded procedure; it is not evidence that the upgrade is executable or that
the coordinator is healthy.

## Approval boundary

An operator must approve the exact release pair and operation `fixture:v0.3.8-to-v0.3.9:001`
after reviewing the generated contract and its immutable release evidence.
The coordinator remains work closed from quiescence through validation. Do not
approve a retry for an interrupted external operation; reconcile its durable
operation record first.

## Procedure

1. Run the read-only contract check and confirm its result is valid and not executable.
2. Run every phase in the order below, recording each durable operation outcome.
3. Acquire and retain the maintenance barrier through replacement and validation.
4. Do not reopen work unless validation succeeds and the known release identity is confirmed.

| Order | Phase | Operation | Failure action |
| ---: | --- | --- | --- |
| 1 | `discover` | `release.inspect` | stop-before-mutation |
| 2 | `preflight` | `admission.check` | stop-before-mutation |
| 3 | `quiesce` | `barrier.acquire` | stop-before-mutation |
| 4 | `backup` | `backend.backup` | stop-before-mutation |
| 5 | `stage` | `runtime.stage` | restore-known-good |
| 6 | `commit` | `authority.atomic_replace` | restore-known-good |
| 7 | `validate` | `runtime.validate` | restore-known-good |
| 8 | `reopen` | `barrier.reopen` | safe-mode |

## Backup and rollback

Before staging or replacement, verify a complete `sqlite` backup and its
restore-equivalence evidence. The backup must remain available until reopen is
validated. On any post-backup failure, restore the known-good release and
authority, validate that restoration, and only then reopen. If restoration or
validation is ambiguous, preserve the barrier and enter safe mode with work
closed; operator inspection is required. The contract's rollback operation is
`backend.restore`; it is not an invitation to invent an alternate command.

- Use the online SQLite backup API (or a clean checkpoint); never copy only the main database while writers are active.
- Retain the database, WAL/SHM companions, binding, selector, and projections in the backup record.
- After restore, run integrity and authority-compatible round-trip checks, including WAL/SHM identity checks.

## Interruption

After a process or host failure, inspect live holders, the durable operation
record, release selector, authority revision, and fencing identity. reconcile
the recorded operation exactly once. A successful health check alone does not
prove recoverability, backup completeness, release correctness, or rollback
equivalence.

# Agent runbook: v0.3.8 to v0.3.9

Generated operation: `fixture:v0.3.8-to-v0.3.9:001`  
Selected backend: `sqlite`

## Non-negotiable rules

- Treat the validated contract as the only source of phase order and operation IDs.
- Keep work closed while the barrier is held; every mutating action carries its fence.
- Never invent a missing prerequisite, identity, backup result, or operation outcome.
- Never retry an interrupted external action before durable reconciliation.
- A health check is an observation only; it does not establish recoverability or correctness.

## Agent execution loop

1. Inspect and validate the contract without mutation.
2. For each phase, verify predecessor completion, exact operation identity, and durable evidence.
3. Stop before mutation when a prerequisite, identity, or backup check fails.
4. After replacement, validate release, authority, backend, projections, revisions, and
   fencing state.
5. Reopen only after validation; otherwise restore and validate the known-good state or
   record safe mode.

## Backend evidence

- Use the online SQLite backup API (or a clean checkpoint); never copy only the main database while writers are active.
- Retain the database, WAL/SHM companions, binding, selector, and projections in the backup record.
- After restore, run integrity and authority-compatible round-trip checks, including WAL/SHM identity checks.

## Failure and recovery

Record the failure against the current operation ID. Preserve old and staged
release identities, the barrier, backup, and journal. reconcile only to a
validated old state, validated new state, or explicit safe mode. Do not
claim completion from partial logs, process liveness, or a passing health check.

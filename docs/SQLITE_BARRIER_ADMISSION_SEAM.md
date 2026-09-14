# SQLite barrier admission seam

Status: design/test contract only. This document does not enable an upgrade,
bind `storage_backend`, or change the default unprovisioned coordinator path.
`handoffctl upgrade apply` and `rollback` remain rejection-only.

## Ownership and lock order

The caller owns one non-reentrant outer scope for the entire authority
operation. It acquires and releases locks in exactly this order:

```text
repository-common -> control-store -> authority
```

`SQLiteBarrierSessionStore.operation_lock()` must not be composed with this
scope: that method acquires its own common/control locks and would either
re-enter them or release the barrier before the authority lock is acquired.
The future adapter therefore needs a caller-owned form with these semantics:

```text
with common_lock:
    with control_store.lock_owned_by_caller():
        session = control_store.recheck_held_locked(expected_identity, expected_revision)
        authority_identity = reread_authority_identity()
        session.require_authority_revision(authority_identity.revision)
        with authority_lock:
            session = control_store.recheck_held_locked(expected_identity, session.revision)
            open_authority_transaction(authority_identity)
            perform_one_mutation()
```

The `*_locked` operations require proof that the current thread owns the
control lock. They must never acquire or release the common or control lock,
and must reject re-entry, a missing owner, stale revision, stale session
identity, project mismatch, non-`held` status, and authority-revision drift.
The second recheck is required after the authority lock is acquired and before
the SQLite transaction opens.

## Failure and durability contract

Any failed or uncertain control-store, authority, transaction, WAL/SHM, or
reopen observation rejects the mutation. It must not release the session or
silently retry a CAS. The caller preserves `held`, `releasing`, or
`ambiguous` safe mode according to the durable session transition contract.
Only an independently verified `released` session may admit a later operation.

The seam must not expose a generic status callback or accept an arbitrary
authority revision supplied by the caller. The authority revision must come
from a descriptor/identity-safe trusted rereader, and the session identity,
fencing token, owner, attempt, and revision must remain bound for the complete
scope.

## Required test scaffold before wiring

The implementation slice may be promoted only with tests that demonstrate:

- caller-owned lock order and non-reentrant/missing-owner rejection;
- stale CAS, session identity, owner, fencing-token, project, and authority
  revision rejection before opening the authority transaction;
- a second recheck after authority-lock acquisition;
- all SQLite write routes entering the same scope, with no writes after a
  rejection;
- control DB, database, WAL, and SHM replacement rejection while descriptors
  are retained;
- process termination or uncertain commit entering durable `ambiguous` state;
- writes resuming only after an independently verified `released` state.

Until this matrix and the corresponding bounded formal refinement exist, the
seam is documentation/test scaffolding only and no mutation route may bind to
it.

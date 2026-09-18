# Upgrade execution redesign

Status: proposed v10 design checkpoint; no mutating upgrade capability.

This document resolves the execution-order and identity conflicts that prevent
the typed upgrade contract from being safely dispatched. It deliberately does
not enable `handoffctl upgrade apply` or `handoffctl upgrade rollback`.
Implementation is allowed only after the dependent ownership, tests, and formal
refinement described below are complete.

## Safety objective

At every observable point, including process death and ambiguous I/O, either
the known-good coordinator accepts work or a durable project-scoped barrier
rejects all authoritative writes. There is no interval in which a failed
forward operation has released its fence but rollback has not acquired one.
No snapshot, verifier, or release delegate may create admission evidence as a
side effect.

## Identity correction

V9 incorrectly coupled the shared barrier identity to a child operation's
`target`. A forward operation has `target=new`, while its failure rollback has
`target=rollback`; one immutable control row therefore cannot represent both.
V10 separates a target-neutral barrier session from immutable child execution
envelopes.

A barrier session record contains:

- `schema_version`, `project_id`, and a fresh `attempt_id`;
- `state_revision` and `authority_revision_at_acquire`;
- `durable_barrier_id`, `fencing_token`, and `fencing_owner`;
- `status` and monotonic control-store `revision`.

Its immutable identity digest covers the first three bullets and no mutable
status, control revision, or child target. Each CAS separately binds the
identity digest plus the expected status and control revision, so transitions
cannot invalidate child envelopes or erase concurrency checks. The forward
child has a distinct `operation_id` and immutable `target=new`. A failure
rollback has another `operation_id` and immutable `target=rollback`. Both
envelopes bind the same `attempt_id`, barrier digest, durable barrier ID,
fencing token, and owner. A later operator-requested rollback after a completed
upgrade is a new attempt with a newly acquired barrier and fresh revisions; it
does not reuse the completed forward barrier.

Target remains mandatory in every phase context, journal record, admission
snapshot, and child envelope. Only the shared barrier session is
target-neutral. Canonical encodings are versioned, length-bounded JSON objects
with exact field sets, sorted keys, UTF-8, and no inferred or omitted values.

## Durable state machines

The barrier session has these CAS-only transitions:

```text
absent -> held -> releasing -> released
             \                 /
              -> ambiguous <---
```

`ambiguous` is durable safe mode. It is never automatically released,
reclaimed, or converted from elapsed time. Reconciliation requires evidence
for the same attempt, barrier, fence, control revision, and authority state.

The forward journal advances in this order:

```text
planned -> discovered -> preflight_verified -> barrier_acquired
 -> quiesced -> backup_verified -> runtime_staged -> selector_committed
 -> target_validated -> reopen_verified -> completed
```

Before `barrier_acquired`, failure has no authoritative effect. At or after
`barrier_acquired`, failure either starts the rollback child while retaining
the barrier or leaves the barrier held/ambiguous in safe mode.

The failure-rollback journal advances in this order:

```text
rollback_planned -> restore_started -> rollback_verified
 -> reopen_verified -> rollback_completed
```

`restore_started` is durable before restoration. Recovery never repeats a
restore merely because completion was not observed; it first reconciles the
artifact, selector, and authority outcome. The only successful terminal
cross-products are:

- forward `completed`, barrier `released`, selector and validated target
  `new`;
- rollback `rollback_completed`, barrier `released`, selector and validated
  target `rollback`.

All other interrupted combinations retain or ambiguously retain the fence.
In particular, forward failure followed by rollback has no transient
`released` state.

## Lock and control-store API

One outer operation scope owns locks in the total order:

```text
repository-common lock -> upgrade control-store lock -> authority lock
```

Methods used inside that scope are explicitly unlocked internal methods and
cannot reacquire an outer lock. Re-entry and reversed acquisition fail before
mutation. A production API has typed values rather than mappings or callbacks:

```text
begin_attempt(forward_envelope) -> HeldBarrierLease
bind_child(held_lease, rollback_envelope) -> BoundRollbackChild
recheck_held(held_lease, fresh_authority_revision) -> HeldBarrierLease
begin_reopen(held_lease, verified_child_evidence) -> ReleasingBarrierLease
complete_reopen(releasing_lease, fresh_runtime_evidence) -> ReleasedBarrierLease
mark_ambiguous(lease, bounded_cause_code) -> AmbiguousBarrierLease
```

Every transition compares the expected project, attempt, barrier digest,
fence, owner, status, and control revision. `bind_child` permits only the
forward-to-failure-rollback relationship and never acquires or replaces the
barrier. `begin_reopen` requires durable validation evidence for the terminal
child. `complete_reopen` requires a fresh authority/runtime reread performed
after the `releasing` record is durable. An I/O failure while publishing either
release transition records or returns ambiguity; it is never reported as an
ordinary unchanged result.

## Engine ordering and opcode dispatch

The existing uniform `snapshot -> admit -> execute` sequence cannot implement
quiescence: it would require a snapshot callback to mutate the barrier or
would admit fabricated pre-barrier evidence. V10 uses a closed protocol for
each allowlisted opcode:

- `release.inspect` and `admission.check` are read-only and precede mutation.
- `barrier.acquire` first commits the control-store barrier under the common
  lock, then drains/fences work, then obtains and admits a fresh quiescence
  snapshot.
- `backend.backup` and `runtime.stage` recheck the held barrier and authority
  before and after their bounded operation.
- `authority.atomic_replace` admits exact current quiescence, performs one
  selector CAS, and records post-rename uncertainty as ambiguous.
- `runtime.validate` fresh-loads the selected runtime, binding, backend,
  authority, and projections.
- `backend.restore` runs only for the bound rollback child under the inherited
  barrier, after durable `restore_started`, and produces independently verified
  restore evidence.
- `barrier.reopen` validates the terminal child, durably enters `releasing`,
  rereads runtime and authority, and only then durably enters `released`.

Production dispatch accepts neither caller-provided handlers nor scripts nor
paths. A built-in adapter derives all locations from the permanent project
binding and a fixed versioned runtime layout. Git execution remains rejected
before mutation until its complete adapter and restore equivalence are
implemented.

Barrier acquisition records the bounded set of wrapped commands that were
already outside the lock. New commands cannot start after the barrier CAS.
Those pre-existing commands may finish and fsync their existing local result
journals, but their authoritative result append is deferred and reconciled
only after verified reopen. Quiesced admission requires that the recorded set
has exited or reached a bounded, explicitly reconciled terminal outcome; it
cannot discard or silently mark an in-flight command complete.

## Fencing SQLite authority writes

The provisioning, identity, lock-order, migration, and privacy contract for
this boundary is specified in
[`CONTROL_STORE_PROVISIONING.md`](CONTROL_STORE_PROVISIONING.md).

Every SQLite-authority mutation, including lifecycle changes, command-result
append, observation updates, migrations, and mutating reconciliation, must use
the outer operation scope. Under the common and control locks it reads the
durable barrier before opening its authority transaction. `held`, `releasing`,
or `ambiguous` rejects the write with a stable error. Process death releases
the OS lock but cannot clear the durable control row, so a new process still
rejects writes. Permitted read-only operations must be enumerated and must not
perform projection repair or implicit migration.

Compatibility requires explicit provisioning. Existing installations without
an upgrade-control-required marker continue normal coordination but cannot
start an upgrade. A separately reviewed, one-time atomic provisioning step
creates and validates the control database and sidecars before installing the
marker. After the marker exists, a missing, aliased, corrupt, or incompatible
control store fails closed. Upgrade code never silently creates a replacement
control store.

This changes the current architecture in which the common lock protects only
SQLite projections. AR-0004 must own the admission/write-fence transition and
its compatibility migration; AR-0007 may consume it but must not duplicate it.

## Selector-aware stable launcher

The selected runtime must be consumed, not merely published. A stable
bootstrap outside the versioned replacement set reads the selector from one
fixed owner-only location and resolves a bounded release token beneath a fixed
`.runtime/releases` directory. It rejects symlinks, hard-link aliases,
unexpected ownership or modes, manifest mismatch, unallowlisted files, and
changes between validation and execution. It passes the independently
validated state root to the selected runtime, which revalidates permanent
project binding before command dispatch.

Staging requires an immutable manifest and source/tag identity; tag signatures
are optional, and a
concrete trust-policy verifier. Contract hashes are assertions, not
authenticity evidence. Flat vendoring cannot be the atomic upgrade mechanism;
the old runtime remains intact, staging populates a distinct versioned
directory, and only the selector CAS changes the active release. The stable
bootstrap itself is outside the upgrade replacement set.

The repository has no current owner or complete API for this bootstrap,
versioned runtime store, authenticity policy, and validation-to-exec binding.
A new dependency AR is therefore required. Mutating CLI commands remain
disabled until that AR is implemented and independently reviewed.

## Required hostile and crash tests

Implementation acceptance requires public-boundary tests for at least:

- barrier CAS occurring before quiescence admission and no mutation from any
  snapshot or verifier;
- distinct immutable forward and rollback child IDs under one continuously
  held barrier session;
- stale control revision, owner, fence, attempt, digest, and target rejection
  for acquire, recheck, child binding, and reopen;
- multiprocess SQLite write rejection while held, after killing the upgrader,
  during `releasing`, and in `ambiguous`, with writes resuming only after
  verified `released`;
- draining in-flight wrapped commands while preventing new claims, mutation,
  reconciliation publication, and migrations;
- missing/corrupt control storage and main/WAL/SHM identity swaps;
- launcher execution of the selected new or rollback release plus manifest,
  signature, symlink, hard-link, ancestor-swap, and selector-fsync faults;
- process death before and after every journal, control-store, selector,
  backup, restore, validation, and reopen durability boundary;
- reconciliation proving that restore is not blindly repeated;
- Git and unsupported schema/backend combinations rejecting before any file,
  journal, database, or selector mutation.

End-to-end campaigns must start from a released coordinator, inject each fault,
restart through the stable bootstrap, and prove either the exact successful
terminal cross-product or write-closed safe mode. `upgrade apply` and
`upgrade rollback` stay fail-closed until this entire matrix is green.

## Formal refinement impact

The upgrade model must introduce at least `barrierStatus`, `barrierAttempt`,
`barrierRevision`, `fence`, `forwardPc`, `rollbackPc`, `childTarget`,
`selector`, `writer`, and `journalOutcome`. Its actions include acquire,
quiesce, bind rollback child, begin/complete reopen, start/verify restore,
SQLite write, crash, and reconcile. TLC must check:

- a SQLite write is accepted only when no required barrier exists or its
  status is `released`;
- at most one active barrier session exists per project;
- each child target and operation identity are immutable;
- rollback after forward failure has no unheld interval;
- durable `rollback_verified` prevents repeated restore;
- the new selector is observable only under a held barrier until validation;
- every successful terminal state has exactly one allowed journal/barrier/
  selector/authority cross-product;
- the lock order is acyclic and outer operation scopes are non-reentrant.

Liveness may assume fair scheduling and successful bounded I/O. I/O-fault and
ambiguous states are intentionally safe-mode terminal until explicit operator
reconciliation; they must not be hidden by a liveness assumption. The current
formal evidence does not model this automaton and cannot be cited for V10.
AR-0008 must provide exact-head model/evidence binding after implementation.

## Ownership and publication gate

- AR-0007 owns the typed engine and control APIs after this normative identity
  correction is accepted.
- AR-0004 owns quiescence, forward acquire/recheck/reopen, and SQLite write
  fencing/provisioning.
- AR-0005 and AR-0006 retain Git and SQLite restore-equivalence ownership.
- AR-0008 owns the new automaton and implementation correspondence evidence.
- A new child AR must own the stable selector-aware bootstrap, versioned
  runtime store, release authenticity verifier, and compatible vendor layout.
- AR-0010 owns the operator reconciliation and safe-mode runbook.

Publication remains blocked until those dependencies, the hostile test matrix,
the formal refinement, independent exact-head review, and a fresh-clone
upgrade-and-rollback campaign all pass. This document authorizes design work
only and does not upgrade any capability claim.

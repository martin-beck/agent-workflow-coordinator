# Correctness-first upgrade protocol

This document defines the coordinator release-upgrade boundary. It is a
contract for the upgrade engine and release-specific plans; it is not evidence
that an upgrade has been executed. Release plans must validate against
`schema/upgrade-contract.schema.json` and bind every operation to a durable
`operation_id`.

## Safety contract

The coordinator remains usable at every externally observable point. Before
authoritative state changes, the protocol must prove prerequisites,
quiescence, release authenticity, compatibility, and a complete restorable
backup. A missing, stale, corrupt, or ambiguous result fails closed.

The authoritative state is never exposed as a partially installed vendor,
binding, backend, or projection. Replacement is a single commit boundary:
until it commits, the verified old runtime is selected; after it commits, the
new runtime is selected only after validation. A failed validation restores the
old runtime and verifies it before reopening work.

## Phases and failure boundaries

Every generated contract contains these ordered phases:

1. `discover`: identify the release, immutable source commit, and operation.
2. `preflight`: check prerequisites, compatibility, clean refs, authority,
   leases, processes, disk, tools, and rollback capacity.
3. `quiesce`: acquire a durable maintenance/admission barrier, fence stale
   workers and leases, drain wrapped commands, and stop new work.
4. `backup`: capture complete Git or SQLite authority plus binding, backend
   selector, projections, refs, and task history; verify integrity and restore
   equivalence before continuing.
5. `stage`: prepare the new runtime outside authority and verify manifests,
   signatures, vendor hashes, schemas, and generated release steps.
6. `commit`: atomically select the staged runtime while the barrier remains
   held. No ambiguous external command may be retried without its durable
   operation ID being reconciled.
7. `validate`: verify runtime, binding, backend, projections, task revisions,
   leases, refs, and authority-compatible Git/SQLite round-trip.
8. `reopen`: release the barrier only after validation succeeds; otherwise
   restore and validate the known-good runtime or enter explicit safe mode.

No phase may silently skip its predecessor. `commit` is the only phase
allowed to replace the selected runtime, and it is never allowed before
verified backup and quiescence.

The schema and contract tests reject duplicate or non-contiguous phase orders,
unknown or forward dependencies, missing phase operations, unbounded time or
resource declarations, and incomplete release identity. Every release binds
its version to a full source commit, tag reference and object, signature,
trust-policy digest, and vendor-manifest digest. Every backend contract must
name its authority, backup, restore, selector, projections, and
authority-compatible round-trip evidence; SQLite additionally declares WAL
handling. A generated plan must also prove that only `commit` mutates
authority, that `commit` depends on both quiescence and backup, and that
`reopen` depends on validation.

## Invariants

- The maintenance/admission barrier is durable and held from quiescence
  through replacement, validation, and reopen or safe-mode decision.
- Active workers, leases, wrapped commands, reconciliation, and publication
  are either drained and fenced or remain on the known-good runtime; none is
  silently discarded.
- Every worker/process action carries a fencing token and stale tokens cannot
  mutate state after quiescence or rollback.
- Read/write availability is explicit in every phase. Read-only inspection may
  continue only where the contract permits it; authoritative writes stop until
  reopen validation passes.
- Git backups include reachable objects, refs, index/worktree metadata,
  binding, backend selector, generated projections, and task history. SQLite
  backups include the database, WAL-consistent backup, binding, selector, and
  projections. Both require integrity and authority-compatible round-trip
  evidence.
- Every external operation has a durable operation ID and outcome. An
  interrupted or ambiguous result is reconciled, never blindly repeated.
- Crash recovery at every phase is idempotent. Recovery either observes the
  old runtime, completes a validated new runtime, restores the old runtime, or
  enters safe mode with work closed.
- Product worktrees and unrelated task history are outside the rollback set.

## Child contracts

AR-0003 owns release-contract generation and completeness for every release.
AR-0004 owns admission, quiescence, fencing, and reopen gates. AR-0005 and
AR-0006 own complete Git and SQLite backup/restore equivalence. AR-0007 owns
the executable phase machine only after those contracts pass. AR-0008 binds
these invariants and failure injections to exhaustive formal models. AR-0009
and AR-0010 own release publication and operator runbooks.

The coordinator must not advertise an upgrade capability until the combined
child contracts, implementation, formal model, fault-injection suite, and a
fresh-clone rollback campaign are independently reviewed and green.

# Correctness-first upgrade protocol

This document defines the coordinator release-upgrade boundary. It is a
contract for the upgrade engine and release-specific plans; it is not evidence
that an upgrade has been executed. Release plans must validate against
`schema/upgrade-contract.schema.json` and bind every operation to a durable
`operation_id`.

The mandatory fail-closed semantic validator is
`tools/validate_upgrade_contract.py`. A generator must invoke it and refuse to
produce an executable plan when it reports an error. JSON Schema provides the
shape and required fields; the validator additionally proves dependency
references and ordering, operation-ID binding, non-no-op release identity,
backend coverage, and backend-specific rollback integrity.

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

## Journal schema compatibility

The exact-v9 engine writes journal schema v3. Schema-v2 journals are detected
and refused before mutation because they do not contain the complete v9
envelope or the durable `rollback_verified` release boundary. The engine never
infers those fields, rewrites an in-flight schema-v2 journal, or treats its
top-level rollback boolean as release evidence.

An operator encountering schema v2 must retain the journal and backup, use the
known-good coordinator runtime that created it to reconcile the operation to a
terminal state, and independently verify the authority before starting a new
schema-v3 operation with a new operation ID and fencing token. An unresolved
or unavailable originating runtime remains in safe mode; there is no automatic
in-place migration path.

## SQLite control-store provisioning boundary

The rollback control database must live in a dedicated directory owned by the
coordinator process effective user with mode `0700`. The main database, lock,
and SQLite `-wal`/`-shm` sidecars are opened without following symlinks. Any
pre-existing sidecar must be a single-link regular file; its device/inode
identity is bound after WAL activation and rechecked before the connection is
closed. A missing, aliased, replaced, or unreadable sidecar fails closed.

This is an explicit deployment trust boundary for the standard-library SQLite
VFS: unrelated code running as the same operating-system user can bypass
advisory locks and replace files in an owner-writable directory. Such code is
trusted to the same extent as the coordinator process. Deployments that need
protection from mutually hostile same-UID processes require separate OS users
or a reviewed descriptor-native custom VFS; they must not weaken the directory
or sidecar checks.

The upgrade delegate cannot authorize rollback release. SQLite binding also
requires a separate authority/runtime rereader, which returns typed facts from
a fresh authority, selector, integrity, foreign-key, fencing, and backend
round-trip inspection. The control adapter validates those facts against the
operation envelope and constructs release evidence itself. Until a production
rereader can obtain the exact release-specific authority revision and runtime
identity, binding fails closed. Git control binding remains unavailable.

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

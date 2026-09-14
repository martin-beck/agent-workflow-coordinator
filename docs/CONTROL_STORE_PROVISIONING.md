# Upgrade control-store provisioning and authority fencing

This contract defines the boundary between the coordinator authority SQLite
database and the durable upgrade-control database. It is a design contract for
AR-0007; it does not enable upgrade execution or make `apply` or `rollback`
permissive. Until every gate below is implemented and independently tested,
those operations remain rejection-only.

## Provisioned objects

Provisioning is project-scoped and one-time. It must run from the permanent
project binding and create the following owner-only objects in a fixed,
versioned runtime directory:

- the authority database, which remains the source of coordination state;
- the control database, containing the barrier/session schema and its WAL and
  SHM sidecars; and
- an `upgrade-control-required` marker containing the project ID, schema
  version, control database identity digest, and provisioning timestamp.

The containing directory, database, marker, lock file, and sidecars must be
owned by the coordinator user and not be group- or world-writable. Paths must
be regular, non-symlinked files beneath the project-bound runtime directory.
Provisioning must use exclusive creation and fsync the database, sidecars,
marker, and containing directory before reporting success. It must never
replace an existing object or silently create a replacement after a failure.

The marker is valid only when all of these values match the permanent project
binding: project ID, state repository, product repository, backend (`sqlite`),
control schema version, and the control database device/inode identity. The
control database must contain the same project ID and schema version, a valid
schema digest, and a valid WAL configuration. The marker and control record
are immutable identity evidence; mutable barrier state is protected by the
control database's revision/CAS protocol.

## Lock and transaction order

Every authority mutation uses one fixed order and holds the scope through the
final commit or rollback:

1. coordinator common lock;
2. project control lock;
3. authority descriptor/transaction lock; and
4. durable control read and authority transaction, with the barrier reread
   immediately before the authority transaction opens and immediately before
   commit where SQLite permits it.

No path may acquire the common lock after the control lock, acquire the
authority lock before the control lock, or call a non-reentrant operation
scope. Read-only inspection must be explicitly classified and may not repair
projections, migrate schemas, change selectors, or create files.

The control read must verify the marker, descriptor identities, WAL/SHM
sidecar identities, project binding, barrier identity, status, revision, and
fencing token. `held`, `releasing`, and `ambiguous` reject the authority write
with stable fail-closed errors. A missing required barrier or an unreadable
control record also rejects the write; absence is not interpreted as
`released`.

## Initialization and migration

An installation without the marker remains usable for ordinary coordination,
but cannot start an upgrade. A reviewed provisioning command must first prove
that no upgrade is active, no mutating reconciliation is in flight, and the
authority passes integrity and binding checks. It then creates the control
store, writes and verifies the schema/digest, fsyncs all objects, and commits
the marker atomically under the same common lock. Re-running provisioning is
idempotent only when every identity and digest matches; otherwise it fails
closed.

Schema migration is a planned, versioned operation under the common-to-control
lock order. It must write a migration journal and backup, validate the new
schema and WAL sidecars, fsync, and atomically advance the marker schema
version. A failed or interrupted migration leaves the old known-good schema
usable or places the coordinator in explicit safe mode; it must never expose a
partially migrated control store. Upgrade code must not perform implicit
migration while opening an authority transaction.

## Process death and failure behavior

OS locks may be released by process death, but durable `held`, `releasing`, or
`ambiguous` state must survive it. A new process must reread the control row
and reject authority writes until an independently authorized recovery path
establishes fresh authority/runtime evidence and commits a valid release. WAL
or SHM disappearance, replacement, inode/device changes, checksum or schema
mismatch, failed fsync, SQLite busy/IO/corruption errors, and uncertain commit
outcomes all fail closed. They must not be converted to a successful release
or a retry that bypasses the barrier.

Recovery and rollback retain the barrier until validation is complete. No
rollback binding may be created after terminal verification, and no new
writer may be admitted during drain, release, or ambiguous handling. These
rules are rejection-only until multiprocess, WAL-recovery, and process-death
tests demonstrate the complete contract.

## Privacy and publication boundary

The marker and control records may contain only stable project identities,
schema/digest values, revisions, fencing tokens, release identifiers, and
sanitized status/error codes. They must not contain user names, absolute home
paths, command arguments, prompts, credentials, environment values, raw
subprocess output, or private repository URLs. Public evidence may report
hashes, schema versions, invariant names, and pass/fail classifications, but
not the private control database contents or filesystem layout.


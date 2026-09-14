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

- the existing, project-bound authority database, which remains the source of
  coordination state and is verified rather than created or replaced;
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
control schema version, and the control database device/inode identity. Its
immutable identity digest binds the authority and control database identities,
the concrete lock-file identity, fixed runtime selector identities, and the
runtime-directory identity. It does not bind ephemeral WAL/SHM inode values.
Those values are recorded in a separate durable, fsynced WAL lifecycle record
described below. The marker also records an opaque, privacy-safe project
identifier. The control database must contain the same project ID and schema
version, a valid
schema digest, and a valid WAL configuration. The marker and control record
are immutable identity evidence; mutable barrier state is protected by the
control database's revision/CAS protocol.

Repository-scoped IDs have one canonical encoding: lowercase ASCII
`sha256:<64 lowercase hexadecimal digits>` over the UTF-8 bytes of a canonical
JSON object whose keys are sorted, separators are `,` and `:`, and values are
restricted to the documented project/repository identity fields. The digest
input contains no URL, local path, username, hostname, credential, or
environment value. A repository-scoped display ID may be a separately
assigned opaque identifier, but it is never used in place of the digest.

Provisioning must bind and verify an existing authority database before it
creates a control store. It must open the authority and every existing
sidecar with no-follow descriptors, verify regular-file type, owner, link
count, device/inode, WAL mode, schema, project binding, and integrity, then
retain those descriptors (or re-open and compare them under the common lock)
for the operation. It must never create, replace, rename, or repair the
authority database or its sidecars as a provisioning side effect.

## Lock and transaction order

Every authority mutation uses one fixed order and holds the scope through the
final commit or rollback:

1. coordinator common lock and its retained directory descriptor;
2. project control lock and its retained lock-file descriptor;
3. the separate project authority lock file (`authority.lock`), opened with
   no-follow and retained as a descriptor; and
4. retained authority descriptors and the SQLite authority transaction; and
5. durable control read and barrier reread under the full scope.

The authority lock is a concrete regular file beside the authority database,
owner-only and created only by provisioning. Its device/inode, parent
identity, owner, link count, and mode are recorded in the marker. Acquisition
uses an exclusive OS file lock on the retained descriptor; release unlocks
that descriptor only after SQLite commit or rollback and identity checks. The
lock file is not the SQLite transaction: both are required, and either
acquisition or descriptor validation failure rejects the mutation.

Each lock object has an identity tuple (device, inode, regular-file type,
owner, link count, and expected parent-directory identity). Before every
critical read and before commit, the implementation compares retained
descriptor identities and the no-follow parent/ancestor chain. A path-name
recheck alone is insufficient: replacement of an ancestor directory,
database, sidecar, lock file, or runtime selector must fail closed. No path
may acquire the common lock after the control lock, acquire the authority lock
before the control lock, or call a non-reentrant operation scope. Read-only
inspection must be explicitly classified and may not repair projections,
migrate schemas, change selectors, or create files.

The control read must verify the marker, descriptor identities, WAL/SHM
sidecar identities, project binding, barrier identity, status, revision, and
fencing token. `held`, `releasing`, and `ambiguous` reject the authority write
with stable fail-closed errors. A missing required barrier or an unreadable
control record also rejects the write; absence is not interpreted as
`released`. For an already-provisioned installation, an absent authority,
control database, lock file, or sidecar required by its lifecycle record is
corruption or loss and must reject every mutation. Before provisioning, absent control objects are
the sole allowed unprovisioned state; absence of the authority or a mismatch
in an existing authority is always an error.

WAL/SHM have an explicit lifecycle. Before the first WAL open, both may be
absent. During an active WAL connection, SQLite may create or retain both;
their no-follow descriptor identities are recorded in a durable lifecycle
record while the full scope is held. The lifecycle record contains the
authority/control database stable identities, WAL mode, lifecycle state
(`absent`, `active`, or `clean_checkpointed`), sidecar device/inode and size
when present, a generation, and a checksum. It is fsynced before the
corresponding sidecar transition is acknowledged.

A normal, explicitly requested checkpoint/truncate followed by connection
close writes and fsyncs `clean_checkpointed`; sidecars may then disappear.
The next controlled WAL open records a new generation and rebinds the newly
created sidecars under the retained authority lock. Crash reconciliation
accepts only a fully fsynced old or new lifecycle record whose stable database
identity still matches. A torn record, impossible transition, sidecar
replacement/deletion during an active connection, unexpected recreation, or
lifecycle transition without the retained authority lock is corruption and
fails closed. Normal SQLite checkpoint lifecycle is therefore distinguishable
from an attack or torn replacement without putting ephemeral inode values in
the immutable marker digest.

## Initialization and migration

An installation without the marker remains usable for ordinary coordination,
but cannot start an upgrade. A reviewed provisioning command must first prove
that no upgrade is active, no mutating reconciliation is in flight, and the
authority passes integrity and binding checks. It then creates the control
store, writes and verifies the schema/digest, fsyncs all objects, and commits
the marker atomically under the same common lock. Marker publication uses a
private temporary marker in the same directory, exclusive creation, file
fsync, atomic rename, and directory fsync. An absent temporary file before
rename is unprovisioned; a leftover temporary file, invalid marker digest, or
marker/control mismatch is incomplete or corrupt provisioning and blocks
mutation until a reviewed reconcile removes the temporary artifact or
completes provisioning. Reconcile may classify these states but must not
guess or replace authority/control objects.

Re-running provisioning is idempotent only when every identity and digest
matches; otherwise it fails closed. A marker rename or control commit whose
outcome is uncertain is not treated as success: the next process must verify
both durable objects and either establish the exact same committed identity or
enter safe mode.

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
or a retry that bypasses the barrier. Immediately before the authority commit,
the implementation must reread the barrier and perform the authority commit
under the same ordered scope. If either the barrier reread or SQLite commit
returns an error, times out, or has an unknown outcome, the operation is
ambiguous: it must not report success, release the barrier, or retry a write
without fresh evidence. The exact protocol is: hold common, control, and
authority locks; reread and validate the barrier; begin the authority
transaction; reread and validate the barrier again; perform the single
authority commit; then classify only an observed successful commit as success.
If the barrier reread or commit has an unknown outcome, durable control state
remains held or becomes ambiguous, and recovery is required. No code path may
release the barrier until a later process has independently established the
authority commit outcome.

Recovery and rollback retain the barrier until validation is complete. No
rollback binding may be created after terminal verification, and no new
writer may be admitted during drain, release, or ambiguous handling. These
rules are rejection-only until multiprocess, WAL-recovery, and process-death
tests demonstrate the complete contract.

## Privacy and publication boundary

The marker and control records may contain only stable project identities,
schema/digest values, revisions, fencing tokens, release identifiers, and
sanitized status/error codes. Public project identifiers must be opaque,
stable digests or repository-scoped IDs that cannot disclose local paths,
usernames, hostnames, or private URLs. They must not contain user names,
absolute home paths, command arguments, prompts, credentials, environment
values, raw subprocess output, or private repository URLs. Public evidence may
report
hashes, schema versions, invariant names, and pass/fail classifications, but
not the private control database contents or filesystem layout.

# Upgrade recovery model

`UpgradeRecovery.tla` is a bounded abstract model of the v9 upgrade protocol.
It covers ordered preflight/quiescence/backup/stage/commit/validate/reopen
transitions, rollback verification and release, and recovery after a durable
rollback verification followed by barrier release.

It also models an interruption while a barrier is held: recovery converts the
ambiguous/safe-mode state into a rollback-started state before verification.

The model now selects an abstract Git or SQLite backup action per operation,
but does not model backend internals. Fence acquisition is explicit; stale
fence/CAS rejection remains an implementation/refinement obligation rather
than a hidden claim.

The model is AI-generated best-effort design guidance, not an implementation proof or a required
refinement target. It abstracts Git,
SQLite, WAL/SHM, filesystem replacement, process scheduling, and external
commands. The implementation and executable tests provide the operational authority; public
evidence may map concrete behavior to model actions without establishing mathematical refinement.

`HandoffctlUpgradeBarrier.tla` is a separate bounded v10 control-plane model.
It covers the target-neutral barrier session, immutable forward and rollback
child identities, compare-and-swap revisions, write admission, and the
fail-closed `ambiguous` state after an uncertain transition. It intentionally
does not model SQLite VFS/WAL durability, power loss, Python refinement, or
the coordinator authority. Its TLC result is therefore bounded abstract
evidence only; it does not enable `upgrade apply` or `upgrade rollback`.

Run with the pinned TLC verifier used by the repository formal workflow:

```sh
java -cp tla2tools.jar tlc2.TLC -config formal/upgrade/UpgradeRecovery.cfg \
  formal/upgrade/UpgradeRecovery.tla

java -cp tla2tools.jar tlc2.TLC -config formal/upgrade/HandoffctlUpgradeBarrier.cfg \
  formal/upgrade/HandoffctlUpgradeBarrier.tla
```

The model is bounded by the constants in the configuration; no unbounded or
implementation-refinement claim is required or made. Exact run metadata is recorded in
`evidence.json`; the executable-to-model obligations and their current gaps
are recorded in `refinement-map.md`.

## Session intent diagnostic

`UpgradeSessionIntent.tla` is a small bounded contract model for the
session-only durable-intent boundary. It distinguishes a prepared intent,
session commit, intent publication, process loss before and after publication,
conservative unknown-outcome recovery to `ambiguous`, and newer-fence
reconciliation. Its invariants close writes before recovery, preserve
intent/revision coherence, require ambiguous state to remain write-closed, and
require a positive fence for every reconciled held state.

This is diagnostic evidence only. It does not model SQLite VFS/WAL fsync,
Python process death, authority writes, or implementation refinement. A pass
does not establish that the current product persists or recovers these states.
The exact isolated run is recorded in `intent-evidence.json` (56 generated,
27 distinct states, depth 7, all seven invariants passed). AR-0007 and AR-0012
remain gated on executable crash-boundary tests, route fencing, and exact-head
formal correspondence. The model does not reject stale concrete tokens because
the production identity fields are intentionally abstracted; that check is
covered by the product tests and remains outside this diagnostic model.

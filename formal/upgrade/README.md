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

The model is deliberately not an implementation proof. It abstracts Git,
SQLite, WAL/SHM, filesystem replacement, process scheduling, and external
commands. The implementation must provide separate public evidence mapped to
each action and invariant before this model can support publication.

Run with the pinned TLC verifier used by the repository formal workflow:

```sh
java -cp tla2tools.jar tlc2.TLC -config formal/upgrade/UpgradeRecovery.cfg \
  formal/upgrade/UpgradeRecovery.tla
```

The model is bounded by the constants in the configuration; no unbounded or
implementation-refinement claim is made. Exact run metadata is recorded in
`evidence.json`; the executable-to-model obligations and their current gaps
are recorded in `refinement-map.md`.

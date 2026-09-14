# Upgrade recovery model

`UpgradeRecovery.tla` is a bounded abstract model of the v9 upgrade protocol.
It covers ordered preflight/quiescence/backup/stage/commit/validate/reopen
transitions, rollback verification and release, and recovery after a durable
rollback verification followed by barrier release.

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
implementation-refinement claim is made.

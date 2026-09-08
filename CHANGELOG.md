# Changelog

## v0.3.5 - 2026-09-09

- Keep the non-CLI SQLite storage module non-executable and remove its misleading shebang.
- Verify clean vendor snapshots preserve valid shebang and executable-mode combinations.

## v0.3.4 - 2026-09-08

- Refactor Git and SQLite reconciliation below the downstream CCN 14 compatibility ceiling.
- Enforce the pinned Lizard complexity gate in local development and CI.

## v0.3.3 - 2026-09-08

- Allow the exact vendored SQLite coordinator test fixture to contain its required UUID.
- Exercise full initialized-state validation after vendor synchronization.

## v0.3.2 - 2026-09-08

- Keep the vendored runtime version consistent with project release metadata.
- Add a regression that prevents release metadata and runtime version drift.

## v0.3.1 - 2026-09-08

- Enforce the exact Huawei 2026 copyright and SPDX MIT pair on every tracked first-party source.
- Preserve executable shebangs and TLA+ MODULE prologues while checking headers locally and in CI.

## v0.3.0 - 2026-09-08

- Make embedded SQLite 3 WAL with `synchronous=FULL` the default authority for new projects.
- Retain the Git/Markdown backend as explicit opt-in and preserve legacy projects without migration.
- Add project-bound backend selection, strict relational constraints, exact-revision transactions,
  append-only events and durable command results.
- Add explicit, equivalence-checked Git-to-SQLite migration and SQLite-to-Git rollback export.
- Keep GitHub observation and publication optional and downstream of committed local authority.
- Add independent-process races, fault/negative tests and a storage/migration TLA+ refinement.

## v0.2.0 - 2026-09-08

- Share one repository-common lock across every local state worktree.
- Fast-forward clean behind replicas before writes and classify true divergence.
- Retry bounded read-only GitHub observations and classify exhausted failures.
- Preflight wrapped commands, fsync privacy-safe command outcomes before follow-up,
  and distinguish recorded-command reconciliation failures.
- Add the exact-revision `recover-expired` transition with a checked UTC deadline.
- Stage complete vendor snapshots before transactional rename installation.
- Extend TLC coverage to worktree lock identity, expired recovery, and durable
  post-command evidence.


## v0.1.4 - 2026-09-08

- Restrict coordinator commits to their explicit owned paths and preserve unrelated staged work.

## v0.1.3 - 2026-09-08

- Refactor generated-view and privacy helpers to satisfy stricter downstream complexity gates.
- Wrap the formal proof documentation for downstream Markdown policies.

## v0.1.2 - 2026-09-08

- Keep UUID privacy checks active except in required identity and contract files.
- Continue scanning project identity files for credentials, paths, addresses, and private keys.
## v0.1.1 - 2026-09-08

- Include the canonical contract test and tracked profile/binding schemas in every vendor pin.

## v0.1.0 - 2026-09-08

- Extract the shared coordinator runtime, deterministic status renderer and formal models.
- Add permanent project binding checked before normal command execution.
- Add pinned, checksummed, offline-capable vendoring and verification.
- Document integration, extension, agent operation, recovery and proof boundaries.

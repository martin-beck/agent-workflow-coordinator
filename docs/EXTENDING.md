# Extending the coordinator

## Downstream extensions

Keep these in the consuming state repository:

- task-schema constraints and additional metadata;
- project worktree/branch naming rules;
- development, verification and publication policy;
- extra read-only reports derived from task data;
- project-specific tests and CI integration.

A downstream extension must not bypass the repository-common coordinator lock, alter task files
during a read, mutate generated files directly, or monkeypatch a vendored module in production.
Prefer a separate deterministic command that consumes snapshots when the existing optional status
view is insufficient.

## Upstream extensions

Change upstream when behavior affects:

- lifecycle transitions, ownership, revisions or dependencies;
- lock acquisition, timeouts or transaction rollback;
- command execution/recording and replication;
- project binding or trusted identity;
- the tracked profile schema;
- a common generated view or vendor format.

An upstream change needs unit and multiprocess fault tests, compatibility fixtures for all known
profiles, documentation, and a version bump. Update TLA+ whenever the abstract transition, lock,
binding, safety or liveness contract changes.

## Compatibility rules

- Patch releases preserve CLI, record and profile schemas.
- Minor releases may add backward-compatible commands or optional profile fields.
- Major releases may intentionally migrate schemas and require a documented downstream procedure.
- Vendor sync never changes the downstream profile or binding.
- A consuming repository pins one exact tag and commit; ranges and floating branches are forbidden.
- Bug-for-bug compatibility is not required for behavior documented as a defect, but the release
  notes and regression tests must name the correction.

## Adding a profile capability

Propose the smallest deterministic setting, define its type and default, update both JSON Schema and
runtime validation, add enabled/disabled tests, document its effect, and verify it cannot weaken the
binding or concurrency contract. Avoid executable plugin paths in tracked configuration: importing
arbitrary project code into the mutation process would enlarge the trusted computing base.

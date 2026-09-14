# SQLite authoritative mutation routes

This inventory is a read-only contract for the provisioned mutation-fence seam.
It does not enable coordinator mutation or claim that the ordinary backend is
safe without an explicitly bound fence.

| Route | SQLite write set | Admission boundary | Status |
| --- | --- | --- | --- |
| `SQLiteBackend.mutate` | task row, dependencies, event | `SQLiteBackend.transaction()` | fenced when `mutation_scope` is bound |
| `SQLiteBackend.update_observations` | task row, event | `SQLiteBackend.transaction()` | fenced when `mutation_scope` is bound |
| `SQLiteBackend.append_command_result` | command-results row | `SQLiteBackend.transaction()` | fenced when `mutation_scope` is bound |
| `SQLiteBackend.retire` | metadata lifecycle row | `SQLiteBackend.transaction()` | fenced when `mutation_scope` is bound |

The shared boundary is intentionally dependency-injected. A production caller
must bind `MutationFence.mutation_scope`; this slice only proves that every
listed route enters the supplied scope before opening its write transaction.
The unbound backend remains outside the acceptance claim and upgrade
apply/rollback remain rejection-only.

## Rollback control-store routes

The durable rollback/session store has a separate write surface. Every public
writer below acquires the coordinator/control operation scope, or delegates to
a writer that does so. The private helpers are only callable while that scope
is already held.

| Store | Route | Durable writes | Evidence |
| --- | --- | --- | --- |
| `SQLiteRollbackControlStore` | `cas` | barrier row/history | `test_cas_conflict_and_binding_mismatch_fail_closed`, `test_v10_cas_fences_verify_affected_rows_and_recovery_errors` |
| `SQLiteRollbackControlStore` | `begin_release`, `reconcile_release` | barrier status/release journal | `test_release_reconciliation_requires_verified_engine_recovery` |
| `SQLiteRollbackControlStore` | `reconcile_ambiguous` | barrier/history replacement | `test_ambiguous_requires_explicit_newer_reconciliation` |
| `SQLiteRollbackControlStore` | `with_barrier` | barrier acquire/release | `test_with_barrier_holds_coordinator_lock_through_authority_callback` |
| `SQLiteBarrierSessionStore` | `create`, `cas`, `bind_child`, `begin_reopen`, `complete_reopen`, `mark_ambiguous` | session/child rows and history | `test_v10_session_cas_fences_insert_and_update_rows`, `test_v10_durable_session_persists_children_and_reopen` |
| `SQLiteBarrierSessionStore` | `recover_unknown`, `reconcile_ambiguous` | intent/session/history rows | `test_v10_session_intent_recovery_fences_and_requires_newer_fence` |

The inventory proves admission ordering and row-fence tests only. It does not
prove that every future authority route is registered here, that SQLite
WAL/SHM survives arbitrary process death, or that the control-store fence is a
complete implementation refinement of the formal model. Authority mutation,
upgrade apply, and upgrade rollback remain disabled.

## Constructor binding gap (AR-0007; future adapter owned by AR-0013)

`SQLiteRollbackControlStore(path, project_id, authority_path=None)` binds the
control database and descriptor identities, but does not accept a
`MutationFence`/authority admission scope. `SQLiteBarrierSessionStore(control,
authority_revision_reader=None)` inherits the same boundary from its control
store and also does not bind an authority mutation scope. Existing callers and
the rejection-only upgrade paths rely on these compatible constructors.

Requiring a scope in either constructor would be a breaking change; silently
adding an optional scope would not be a fail-closed proof. The exact remaining
route gap is a future typed adapter, owned by AR-0013, that binds the
already-tested `MutationFence.mutation_scope` around every authoritative write
while keeping these control-plane constructors compatible. AR-0007 remains
incomplete until its required fencing/correspondence evidence is accepted; this
PR records the gap but does not satisfy that implementation gate. Until the
adapter and its multiprocess evidence exist, this is an explicit nonclaim.

The uncalled `commit_runtime_selector_admitted` adapter defines the
caller-owned selector boundary: it requires a typed lease exposing `hold()`
and `assert_ordered()`, validates the lease before touching the selector path,
and publishes only while the lease is held. Its tests reject missing,
structurally invalid, and incorrectly ordered leases. This is an API contract
only; no existing caller uses it, and selector mutation remains disabled in
the rejection-only upgrade paths.

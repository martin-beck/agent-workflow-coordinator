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
must bind `MutationFence.mutation_scope`; `bind_sqlite_authority_writer` now
issues a typed capability exposing `mutate`, `update_observations`,
`append_command_result`, and `retire` while entering every listed route under
the caller-owned lock-domain scope. It never passes the raw backend to the
caller and is not exposed through the public upgrade dispatcher.
The unbound backend remains outside the acceptance claim and upgrade
apply/rollback remain rejection-only.

## Rollback control-store routes

The durable rollback/session store has a separate write surface. Every public
writer below acquires the coordinator/control operation scope, or delegates to
a writer that does so. The private helpers are only callable while that scope
is already held.

`bind_sqlite_coordination_writer` adds typed common -> control -> authority
operations for durable control/session transitions without exposing either
store. The current adapter methods are `control_cas`,
`control_begin_release`, `control_complete_release`, `control_with_barrier`,
`session_cas`,
`session_bind_child`, `session_begin_reopen`, `session_complete_reopen`, and
`session_mark_ambiguous`, `session_recover_unknown`,
`session_prepare_authority_effect`, and `session_finish_authority_effect`. They remain an
uncalled coordination seam. `bind_sqlite_coordination_recovery` separately
issues a fresh-fence `reconcile_ambiguous` operation for an ambiguous session;
it does not reuse the old held-session lease. Its `create_session` method
bootstraps an initial held session under the same fresh-fence boundary. Its
`reconcile_control_ambiguous` method applies the same fresh-fence boundary to
an ambiguous rollback-control barrier. Public upgrade mutation and Dispatch
stay rejection-only.

| Store | Route | Durable writes | Evidence |
| --- | --- | --- | --- |
| `SQLiteRollbackControlStore` | `cas` | barrier row/history | `test_cas_conflict_and_binding_mismatch_fail_closed`, `test_v10_cas_fences_verify_affected_rows_and_recovery_errors` |
| `SQLiteRollbackControlStore` | `begin_release`, `reconcile_release`, release completion | barrier status/release journal | `test_release_reconciliation_requires_verified_engine_recovery`, `test_typed_coordination_writer_completes_release_and_reopen` |
| `SQLiteRollbackControlStore` | `reconcile_ambiguous` | barrier/history replacement | `test_ambiguous_requires_explicit_newer_reconciliation`, `test_fresh_recovery_scope_reconciles_ambiguous_control_barrier` |
| `SQLiteRollbackControlStore` | `with_barrier` | barrier acquire/release | `test_with_barrier_holds_coordinator_lock_through_authority_callback`, `test_typed_coordination_writer_with_barrier_holds_full_scope` |
| `SQLiteBarrierSessionStore` | `create`, `cas`, `bind_child`, `begin_reopen`, `complete_reopen`, `mark_ambiguous` | session/child rows and history | `test_v10_session_cas_fences_insert_and_update_rows`, `test_v10_durable_session_persists_children_and_reopen`, `test_typed_coordination_writer_completes_release_and_reopen`, `test_typed_coordination_writer_fences_uncertain_session_outcome`, `test_fresh_recovery_scope_creates_initial_session` |
| `SQLiteBarrierSessionStore` | `recover_unknown`, `reconcile_ambiguous` | intent/session/history rows | `test_v10_session_intent_recovery_fences_and_requires_newer_fence`, `test_typed_coordination_writer_recovers_unknown_under_full_scope`, `test_fresh_recovery_scope_reconciles_ambiguous_session` |
| `SQLiteBarrierSessionStore` | `prepare_authority_effect`, `finish_authority_effect` | authority-effect intent/session rows | `test_typed_coordination_writer_fences_authority_effect_intent` |

The inventory proves admission ordering and row-fence tests only. It does not
prove that every future authority route is registered here, that SQLite
WAL/SHM survives arbitrary process death, or that the control-store fence is a
complete operational evidence for the formal model. Authority mutation,
upgrade apply, and upgrade rollback remain disabled.

## Constructor binding gap (AR-0091; parent ownership remains AR-0004/AR-0013)

`SQLiteRollbackControlStore(path, project_id, authority_path=None)` binds the
control database and descriptor identities, but does not accept a
`MutationFence`/authority admission scope. `SQLiteBarrierSessionStore(control,
authority_revision_reader=None)` inherits the same boundary from its control
store and also does not bind an authority mutation scope. Existing callers and
the rejection-only upgrade paths rely on these compatible constructors.

Requiring a scope in either constructor would be a breaking change; silently
adding an optional scope would not be a fail-closed proof. The typed
coordination seam keeps these control-plane constructors compatible. Remaining
recovery and cross-process route evidence still requires explicit hostile tests;
this slice does not enable public upgrade mutation.

The `SQLiteAuthorityWriteAdapter` is a caller-owned authority seam; it does
not authorize upgrade phases or connect public apply/rollback. The uncalled
`commit_runtime_selector_admitted` adapter defines the
caller-owned selector boundary: it requires an immutable `AdmissionLease`, a
matching `AdmissionRecheck`, and a separate ordered scope that exposes
`assert_ordered()` and `hold()`. It validates immutable evidence and lock order
before touching the selector path, and publishes only while the caller-owned
scope is held. Its tests reject missing, structurally invalid, mismatched, and
incorrectly ordered inputs. This is an API contract only; no existing caller
uses it, and selector mutation remains disabled in the rejection-only upgrade
paths.

The uncalled `AdmissionSession` composes the typed control-store and selector
adapters around one caller-owned lease, exact recheck, and ordered scope. It
acquires the scope separately for each operation, so a failed CAS or selector
publication cannot leave a session-wide lock held. This remains an integration
seam only: no coordinator production caller constructs the session, and no
upgrade or rollback mutation path is enabled by this slice. Its tests cover
stale rechecks, lock-order rejection, release on injected failure, and
cross-process non-overlap in the supplied scope.

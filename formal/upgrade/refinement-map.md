# Upgrade model evidence map

This is an evidence map, not a refinement proof. The model actions identify
the obligations that implementation tests must cover; the current checkpoint
records only the TLC result in `evidence.json`.

## SQLite snapshot safe-mode trace map

`sqlite-snapshot-correspondence.json` maps the existing read-only
`SQLiteCompatibilitySession` to `UpgradeRecovery`: snapshot reads and identity
rereads leave the abstract state unchanged; read or descriptor-close
uncertainty maps to `Crash` from `running`/`held` into `safe_mode`/`ambiguous`;
and the permanent in-memory fence rejects a later `Acquire`. The artifact is
bounded evidence scaffolding, not an implementation-refinement proof. It does
not authorize writes, outcome publication, Git dispatch, apply, or rollback.

The machine-readable `v10-refinement-contract.json` records the same bounded
model/config hashes, implementation snapshot, action-to-obligation mapping,
the exact SQLite route inventory, required executable evidence, and explicit
non-claims. It is a publication contract for the next implementation slice,
not an implementation proof.

| Model obligation | Intended implementation evidence | Status |
| --- | --- | --- |
| integrated ordered transition and functional failure preservation | `test_complete_trace_is_functional_and_mutation_free`, `test_rejects_order_identity_and_mutation_drift`, `test_failure_paths_require_functional_write_closed_recovery`, `test_bound_rehearsal_rejects_trace_identity_drift` | bounded executable integrated rehearsal evidence; implementation refinement pending |
| diagnostic recovery evidence before rollback authorization | `test_diagnostic_recovery_evidence_never_authorizes_rollback`, `test_rejects_forged_or_unverified_recovery`, `test_bound_adapter_rejects_phase_and_identity_drift` | bounded executable diagnostic recovery evidence; implementation refinement pending |
| commit authorization before mutation | `test_prerequisites_are_verified_without_authorizing_commit`, `test_rejects_forged_or_unverified_prerequisites`, `test_bound_adapter_rejects_phase_and_identity_drift` | bounded executable commit authorization evidence; implementation refinement pending |
| immutable direct admission construction | `tests/test_authority_neutral_commit.py::CommitAuthorizationTests.test_admission_bundle_binds_all_mutation_identities` | direct and evidence-derived admission bundles validate all authority, session, artifact, selector, and runtime identities; mutation remains disabled |
| lock-domain bind admission | `tests/test_lock_domain_scope.py::test_bind_rejects_nonheld_durable_session_before_returning_scope` | non-held durable sessions are rejected before a scope is returned; lock order and implementation refinement remain pending |
| ordered lock/recheck correspondence | `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_scope_proves_durable_session_inside_all_three_locks`, `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_hold_releases_locks_when_second_trusted_reread_fails`, `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_hold_rejects_trusted_authority_drift_between_rereads` | concrete common/control/authority ordering, trusted reread, and failure release are hostile-tested; trace refinement remains pending |
| process-death lock release and stale-owner recovery | `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_process_death_releases_scope_for_fresh_recheck`, `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_validated_hold_process_death_releases_scope_for_fresh_caller`, `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_binding_abort_releases_locks_and_fresh_worker_rejects_replaced_identity`, `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_binding_abort_allows_fresh_worker_reacquisition_when_identity_is_unchanged`, `tests/test_lock_domain_scope.py::LockDomainScopeTests.test_repeated_handoff_rereads_authority_before_rejecting_stale_retry` | independent-process lock release, replacement rejection, fresh reacquisition, and repeated trusted reread evidence are mapped; formal implementation refinement remains pending |
| isolated Git authority effect | `tests/test_git_authority_mutation.py::GitCommitCapabilityTests.test_commits_only_pre_staged_change_and_verifies_postconditions`, `tests/test_git_authority_mutation.py::GitCommitCapabilityTests.test_capability_is_single_use_but_fresh_capability_reopens`, `tests/test_git_authority_mutation.py::GitCommitCapabilityTests.test_rejects_unstaged_or_empty_changes`, `tests/test_git_authority_mutation.py::GitCommitCapabilityTests.test_rejects_head_drift_and_ambiguous_runner_outcome`, `tests/test_git_authority_mutation.py::GitCommitCapabilityTests.test_rejects_a_concurrent_commit_after_the_effect` | bounded executable Git effect, single-use, and fresh-capability reopen evidence; implementation refinement pending and public dispatch disabled |
| isolated SQLite authority effect | `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_commits_effect_and_verifies_integrity`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_capability_is_single_use_but_fresh_capability_reopens`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_rejects_sidecar_identity_drift_before_effect`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_classifies_post_commit_identity_drift_as_ambiguous`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_classifies_sqlite_errors_as_ambiguous`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_classifies_connection_close_failure_as_ambiguous`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_classifies_rollback_failure_as_ambiguous` | bounded executable SQLite effect, single-use, and fresh-capability reopen evidence; implementation refinement pending and public dispatch disabled |
| durable external authority-effect outcome | `tests/test_authority_mutation.py::test_durable_effect_publishes_only_after_verified_receipt`, `tests/test_authority_mutation.py::test_durable_ambiguous_effect_is_durably_marked_ambiguous`, `tests/test_sqlite_mutation_barrier.py::test_authority_effect_journal_validates_identity_and_single_use`, `tests/test_sqlite_mutation_barrier.py::test_sigkill_after_authority_effect_requires_recovery_and_new_fence`, `tests/test_rollback_control_store.py::test_v10_subprocess_death_after_committed_effect_publication_is_reopenable`, `tests/test_rollback_control_store.py::test_v10_subprocess_death_after_ambiguous_effect_publication_is_reopenable` | bounded executable prepared-intent, pre-effect fencing, post-publication reopen, process-death, write-closed recovery, and newer-fence evidence; implementation refinement pending and public dispatch disabled |
| integrated single-use authority effect | `tests/test_authority_mutation.py::AuthorityMutationTests.test_effect_is_single_use_and_identity_bound`, `tests/test_authority_mutation.py::AuthorityMutationTests.test_rejects_backend_or_result_identity_drift`, `tests/test_authority_mutation.py::AuthorityMutationTests.test_ambiguous_effect_consumes_token_and_cannot_retry`, `tests/test_authority_mutation.py::AuthorityMutationTests.test_backend_ambiguity_is_normalized_and_cannot_retry`, `tests/test_authority_mutation.py::AuthorityMutationTests.test_termination_exception_is_ambiguous_and_cannot_retry` | bounded executable cross-backend effect evidence; implementation refinement pending and public dispatch disabled |
| runtime replacement admission before mutation | `test_admission_verifies_runtime_and_selector_without_mutation`, `test_rejects_forged_context_manifest_selector_and_backend_failures`, `test_bound_adapter_rejects_phase_and_identity_drift` | bounded executable runtime admission evidence; implementation refinement pending |
| selector publication admission before mutation | `test_admission_is_read_only_and_bound_to_visibility_scope`, `test_rejects_invalid_identity_and_backend_failures`, `test_bound_adapter_rejects_phase_and_identity_drift` | bounded executable selector admission evidence; implementation refinement pending |
| concrete authority admission reread before effect | `tests/test_git_authority_mutation.py::GitCommitCapabilityTests.test_rejects_stale_admission_reread_before_effect`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_rejects_stale_admission_reread_before_effect` | bounded executable stale-admission rejection evidence; implementation refinement pending |
| selector/runtime readiness before reopen | `test_engine_binds_selector_readiness_to_validate_phase`, `test_bound_adapter_dispatches_validate_and_rejects_drift`, `test_revalidation_failure_is_write_closed` | bounded executable selector/runtime readiness evidence; implementation refinement pending |
| forward stage artifact binding | `test_engine_binds_verified_stage_capability_to_stage_phase`, `test_bound_adapter_dispatches_stage_and_rejects_drift`, `test_rejects_artifact_escape_and_bad_digest` | bounded executable stage verification evidence; implementation refinement pending |
| forward backup binding and failure | `test_engine_binds_verified_backup_capability_to_backup_phase`, `test_bound_phase_adapter_binds_identity_and_dispatches_only_backup`, `test_bound_phase_adapter_rejects_identity_drift_before_dispatch` | bounded executable backup binding evidence; implementation refinement pending |
| discover/preflight/quiesce | `test_apply_is_ordered_and_idempotent`, `test_commit_validate_and_reopen_require_safety_evidence` | public tests present; refinement pending |
| backup before commit | `test_apply_adapter_fault_boundaries_are_durable_and_fail_closed`, `test_failure_is_durable_and_rollback_can_enter_safe_mode` | public tests present; refinement pending |
| validate/reopen release order | `test_commit_validate_and_reopen_require_safety_evidence`, `test_releasing_barrier_cannot_be_completed_without_authority_evidence` | public tests present; refinement pending |
| rollback_started/rollback_verified | `test_rollback_verified_is_durable_before_release_and_revalidated`, `test_successful_rollback_requires_and_writes_terminal_record` | public tests present; refinement pending |
| terminal rollback requires durable backup | `test_successful_rollback_requires_and_writes_terminal_record`, `test_failure_is_durable_and_rollback_can_enter_safe_mode` | public tests present; refinement pending |
| `Crash` and ambiguous recovery | `test_sigkill_after_durable_releasing_recovers_without_second_restore`, `test_ambiguous_requires_explicit_newer_reconciliation` | public tests present; refinement pending |
| `FunctionalAvailability` | `test_loaded_rollback_requires_authority_and_runtime_revalidation`, `test_apply_rejects_failed_state_and_unreconciled_rollback_record`, `tests/test_git_authority_mutation.py::GitCommitCapabilityTests.test_capability_is_single_use_but_fresh_capability_reopens`, `tests/test_sqlite_authority_mutation.py::SQLiteCommitCapabilityTests.test_capability_is_single_use_but_fresh_capability_reopens`, `tests/test_sqlite_mutation_barrier.py::test_sigkill_after_authority_effect_requires_recovery_and_new_fence`, `tests/test_sqlite_mutation_barrier.py::test_sigkill_after_git_authority_effect_requires_recovery_and_new_fence` | bounded executable availability and write-closed recovery evidence; refinement pending |
| Git backend | production Git adapter and ref/restore tests | model branch exists; production adapter explicitly fail-closed |
| SQLite backend | route inventory in `v10-refinement-contract.json`; `test_sqlite_mutation_barrier.py` and `test_sqlite_storage.py` | bounded executable route evidence; mathematical refinement remains not-proven |
| Concrete SQLite rereader | `test_concrete_release_rereader_derives_and_rechecks_actual_authority`, selector/projection/sidecar swap tests | public hostile tests present; trace refinement and release orchestration pending |
| Typed bound rollback rejection | `test_rollback_rejects_forged_bound_verifier_before_backend_or_handler`, `test_rollback_rejects_mismatched_capability_before_journal_activity`, `test_bound_rollback_rejects_stale_and_replaced_sessions_before_git`, `test_bound_rollback_rejects_malformed_admission_before_git`, `test_bound_rollback_rejects_stale_and_replaced_sessions_before_sqlite`, `test_bound_rollback_rejects_malformed_admission_before_sqlite` | bounded executable rejection evidence; correspondence and mutation authorization remain not-proven |
| Typed recovery rejection preservation | `tests/test_rollback_control_store.py::RollbackControlStoreTests.test_v10_session_intent_recovery_fences_and_requires_newer_fence`, `tests/test_rollback_control_store.py::RollbackControlStoreTests.test_v10_session_intent_recovery_and_reconcile_reject_reentrant_or_invalid_calls`, `tests/test_rollback_control_store.py::RollbackControlStoreTests.test_v10_interrupted_recovery_residue_requires_exact_revision_chain`, `tests/test_rollback_control_store.py::RollbackControlStoreTests.test_v10_session_intent_recovery_rejects_missing_session`, `tests/test_rollback_control_store.py::RollbackControlStoreTests.test_v10_session_intent_recovery_rejects_invalid_intent_records` | stale/reused fences, authority and call-shape failures, invalid or missing session records, unresolved intent, lock release, and ambiguous snapshot preservation are hostile-tested; implementation refinement remains pending |
| Process-death and ambiguous recovery | `process_death_evidence` in `v10-refinement-contract.json` | exact hostile independent-process mappings cover SQLite and Git authority effects, post-publication committed/ambiguous effect outcomes, plus barrier recovery; implementation refinement remains not-proven |

The names above are resolved against the implementation snapshot recorded in
`evidence.json`; they are not claims about later source revisions. The model
actions are coarser than the Python phase journal, so passing these tests does
not establish a trace-preserving refinement. In particular, no test currently
binds the abstract `Backends` parameter or `fence` variable to a model
transition, and Git remains fail-closed. The model's fence increment is an
abstract admission token only; it is not evidence of implementation CAS or
stale-owner rejection.

The final product snapshot also contains the fail-closed `upgrade check` and
`upgrade plan` commands. Their `apply` and `rollback` paths intentionally
reject before mutation; no opcode is dispatched by this snapshot. Therefore
the model remains bounded design evidence, not execution or refinement proof.

The proposed v10 redesign at the exact design snapshot in `evidence.json`
introduces a target-neutral barrier session, distinct forward/rollback child
operations, explicit acquire/recheck/reopen transitions, SQLite write fencing,
and a selector-aware launcher. None of those v10 transitions are claimed by
the current model or implementation snapshot; a new model and exact-head
refinement run is required after the executable adapter lands.

The model abstracts these mechanisms. A passing TLC run must not be reported
as proof that any row is implemented until the corresponding executable
evidence is independently recorded at the exact product revision.

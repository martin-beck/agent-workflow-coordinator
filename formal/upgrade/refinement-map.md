# Upgrade model evidence map

This is an evidence map, not a refinement proof. The model actions identify
the obligations that implementation tests must cover; the current checkpoint
records only the TLC result in `evidence.json`.

The machine-readable `v10-refinement-contract.json` records the same bounded
model/config hashes, implementation snapshot, action-to-obligation mapping,
required executable evidence, and explicit non-claims. It is a publication
contract for the next implementation slice, not an implementation proof.

| Model obligation | Intended implementation evidence | Status |
| --- | --- | --- |
| discover/preflight/quiesce | `test_apply_is_ordered_and_idempotent`, `test_commit_validate_and_reopen_require_safety_evidence` | public tests present; refinement pending |
| backup before commit | `test_apply_adapter_fault_boundaries_are_durable_and_fail_closed`, `test_failure_is_durable_and_rollback_can_enter_safe_mode` | public tests present; refinement pending |
| validate/reopen release order | `test_commit_validate_and_reopen_require_safety_evidence`, `test_releasing_barrier_cannot_be_completed_without_authority_evidence` | public tests present; refinement pending |
| rollback_started/rollback_verified | `test_rollback_verified_is_durable_before_release_and_revalidated`, `test_successful_rollback_requires_and_writes_terminal_record` | public tests present; refinement pending |
| `Crash` and ambiguous recovery | `test_sigkill_after_durable_releasing_recovers_without_second_restore`, `test_ambiguous_requires_explicit_newer_reconciliation` | public tests present; refinement pending |
| `FunctionalAvailability` | `test_loaded_rollback_requires_authority_and_runtime_revalidation`, `test_apply_rejects_failed_state_and_unreconciled_rollback_record` | public tests present; refinement pending |
| Git backend | production Git adapter and ref/restore tests | model branch exists; production adapter explicitly fail-closed |
| SQLite backend | `test_wal_cas_and_reload_are_durable`, `test_sidecars_require_private_provisioning_and_stable_regular_identities` | model branch and public tests present; release-specific rereader/refinement pending |
| Concrete SQLite rereader | `test_concrete_release_rereader_derives_and_rechecks_actual_authority`, selector/projection/sidecar swap tests | public hostile tests present; trace refinement and release orchestration pending |
| Typed recovery rejection preservation | `test_v10_session_intent_recovery_fences_and_requires_newer_fence` (PR #289) | bounded executable evidence at `99b65e6`; implementation refinement pending |

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

# Best-effort model correspondence map

This document maps executable safety evidence to the bounded upgrade models. It
is not, and does not require, a trace-preserving Python-to-TLA+ refinement
proof. The models may be AI-generated and are maintained as best-effort design
guidance; source code and executable tests are the operational authority.

The machine-readable contract is
[`v10-refinement-contract.json`](v10-refinement-contract.json). Its
`refinement_requirement` is `not-required`, while its mutation gate remains
independent: public upgrade apply and rollback stay rejection-only until the
operational obligations have exact-head executable evidence and independent
review.

## Correspondence boundary

The following mappings are evidence relationships, not equivalence claims:

| Model area | Executable evidence | Current interpretation |
| --- | --- | --- |
| Upgrade lifecycle ordering and failure recovery | `tests/test_upgrade_engine.py`, `tests/test_upgrade_admission.py`, `tests/test_upgrade_campaign.py` | Best-effort action correspondence; concrete failure handling is authoritative. |
| Lock domain, identity rereads, and stale CAS | `tests/test_lock_domain_scope.py`, `tests/test_mutation_fence.py` | Best-effort correspondence with fail-closed rejection on identity drift. |
| Git authority effect boundaries | `tests/test_git_authority_mutation.py`, `tests/test_git_authority_adapter.py` | Concrete pre-effect rejection, ambiguity, recovery, and fresh-capability evidence; dispatch remains disabled. |
| SQLite authority effect boundaries | `tests/test_sqlite_authority_mutation.py`, `tests/test_sqlite_authority_adapter.py` | Concrete pre-effect rejection, ambiguity, recovery, and fresh-capability evidence; dispatch remains disabled. |
| SQLite route fencing and process death | `tests/test_sqlite_mutation_barrier.py`, `tests/test_sqlite_route_inventory.py` | Concrete route and recovery evidence; no model-refinement proof is required. |
| Durable intent and ambiguous recovery | `tests/test_rollback_control_store.py`, `formal/upgrade/intent-evidence.json` | Diagnostic model guidance paired with executable recovery tests. |
| Selector/runtime admission | `tests/test_authority_neutral_selector.py`, `tests/test_authority_neutral_runtime.py`, `tests/test_upgrade_admission.py` | Read-only admission and identity evidence; no mutation authorization. |

Passing TLC establishes only the checked finite model properties and their
declared assumptions. It does not establish source-code equivalence, and a
source change may intentionally refine, approximate, or diverge from the
model when the executable contract and safety evidence are updated accordingly.

The bounded model also excludes SQLite VFS/WAL durability, power loss,
arbitrary process scheduling, external command behavior, and filesystem
failure modes outside the documented executable tests. Those boundaries remain
explicit nonclaims rather than unfinished proof obligations.

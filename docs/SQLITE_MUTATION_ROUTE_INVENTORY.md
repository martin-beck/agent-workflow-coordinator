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

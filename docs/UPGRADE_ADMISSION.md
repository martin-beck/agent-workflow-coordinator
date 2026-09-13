# Upgrade admission contract

Upgrade execution is fail-closed and has three independently checked boundaries:

1. **Preflight** is read-only. Release authenticity, runtime support, clean and
   synchronized authority, backend/binding/vendor validity, absence of active
   leases and wrapped commands, stopped reconciliation/publication, capacity,
   and a restorable backup destination must all be proven.
2. **Quiescence** holds a durable maintenance barrier while workers drain,
   leases fence, wrapped commands drain, and reconciliation/publication stop.
   Replacement is not admitted while any predicate is false or absent.
3. **Reopen** is admitted only after runtime, backend round-trip, projections,
   binding, and lease-fence validation succeed while the barrier remains held.

The predicates in `tools/upgrade_admission.py` are pure checks and do not mutate
authority. Missing keys, false values, and non-boolean truthy values all fail
closed. A later executor must persist each barrier and validation result before
crossing the corresponding boundary.

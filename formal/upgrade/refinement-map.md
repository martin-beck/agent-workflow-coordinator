# Upgrade model evidence map

This is an evidence map, not a refinement proof. The model actions identify
the obligations that implementation tests must cover; the current checkpoint
records only the TLC result in `evidence.json`.

| Model obligation | Intended implementation evidence | Status |
| --- | --- | --- |
| discover/preflight/quiesce | `tests/test_upgrade_engine.py` admission and phase tests | implementation evidence pending |
| backup before commit | `tests/test_upgrade_engine.py`, backup fault tests | implementation evidence pending |
| validate/reopen release order | upgrade-engine and control-store release tests | implementation evidence pending |
| rollback_started/rollback_verified | rollback journal and control-store tests | implementation evidence pending |
| `Crash` and ambiguous recovery | process-death and reconciliation tests | implementation evidence pending |
| `FunctionalAvailability` | runtime admission/reopen tests | implementation evidence pending |
| Git backend | production Git adapter and ref/restore tests | explicitly fail-closed/pending |
| SQLite backend | WAL/SHM and restore-equivalence tests | implementation evidence pending |

The model abstracts these mechanisms. A passing TLC run must not be reported
as proof that any row is implemented until the corresponding executable
evidence is independently recorded at the exact product revision.

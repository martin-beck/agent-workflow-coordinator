# Contributor instructions

Read README.md, docs/ARCHITECTURE.md, docs/EXTENDING.md and the formal contract before changing
runtime behavior. Preserve the offline vendoring boundary and permanent project binding.

Every transition, lock, binding, vendor-format or recovery change requires focused negative-path
tests. Update TLA+ when the abstract contract changes. Run formatting, lint, strict typing, full
branch coverage, source-header verification, schema examples, vendor tests and all TLC models
before publication.

Do not add runtime dependencies, executable downstream plugins, network-required startup, floating
versions, project secrets, machine paths, or private evidence. Commits must be signed and include a
matching DCO Signed-off-by trailer. Publish changes through reviewed pull requests.

Treat exit status 75 and a `RETRYABLE:` handoffctl error as transient coordinator
contention: retry with bounded backoff. Treat exit status 76 and a
`REPAIR_REQUIRED:` error as a required replica reconciliation: preserve any
local receipt/commit, repair the replica, then resume. Neither result permits
marking the task blocked solely because `main` moved, a replica diverged, or a
bounded coordinator lock wait elapsed. Protected-ref and release publication
still require their exact-head checks.

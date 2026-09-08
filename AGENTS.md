# Contributor instructions

Read README.md, docs/ARCHITECTURE.md, docs/EXTENDING.md and the formal contract before changing
runtime behavior. Preserve the offline vendoring boundary and permanent project binding.

Every transition, lock, binding, vendor-format or recovery change requires focused negative-path
tests. Update TLA+ when the abstract contract changes. Run formatting, lint, strict typing, full
branch coverage, schema examples, vendor tests and all TLC models before publication.

Do not add runtime dependencies, executable downstream plugins, network-required startup, floating
versions, project secrets, machine paths, or private evidence. Commits must be signed and include a
matching DCO Signed-off-by trailer. Publish changes through reviewed pull requests.

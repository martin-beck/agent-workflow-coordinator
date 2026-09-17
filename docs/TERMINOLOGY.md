# Coordinator terminology

This registry is coordinator-owned data at `quality/terminology.json`. The
terms below are normative and apply consistently to both supported backends.

- A **task** is one bounded unit of coordination work identified by a stable
  AR identifier.
- **Status** is the task lifecycle value (`planned`, `open`, `in_progress`,
  `blocked`, `done`, `future`, `cancelled`, or `superseded`).
- An **owner** is the stable worker identity currently authorized to act on a
  claimed task.
- A **claim** is the accepted transition that assigns one open task to one
  owner; a **lease** is the bounded expiry attached to that claim.
- A **revision** is the exact monotonic task revision used for compare-and-swap
  mutation admission; it is not a software version.
- The **backend authority** is the selected Git/Markdown or SQLite store that
  authoritatively records coordination data.
- A **projection** is deterministic derived output such as `CURRENT.md` or
  `STATUS.md`; it is never mutation authority.
- A **binding** is the permanent project identity relation among the state
  repository, product repository, profile, and backend selector.
- **Reconciliation** refreshes observations and regenerates projections from
  the backend authority; it does not infer lifecycle completion.
- **Vendor sync** installs an exact released coordinator snapshot offline
  according to the downstream manifest; it does not change project binding or
  backend selection.
- **Task release** is the lifecycle transition that clears ownership and moves
  a task to a non-active status. A **software release** is a reviewed,
  immutable coordinator version and is independent of task release.

The AWQ terminology profile checks the registry's forbidden aliases in the
declared documentation and example scopes. Its findings are bounded lexical
evidence only; native coordinator gates remain authoritative.

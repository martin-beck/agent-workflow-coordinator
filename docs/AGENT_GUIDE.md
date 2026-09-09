# Agent operating contract

For autonomous or interactive agents using a project that embeds handoffctl:

1. Run from the bound product checkout or state repository.
2. Read the project's canonical development policy completely.
3. Run `tools/handoffctl snapshot`; read the full selected task, plan, dependencies, revision,
   checkpoint, branch and worktree.
4. Claim exactly one ready `open` task with a stable unique owner. Never claim planned, blocked,
   completed, dependency-blocked, or already-owned work.
5. Use only the task's named branch/worktree. The wrapped-command preflight rejects a product
   checkout whose Git worktree or branch differs from the active task declaration. Preserve
   unrelated work.
6. Route every product/Git/review/publication mutation through `handoffctl run`.
7. After every material result or failure, update the task immediately. Heartbeat before expiry.
8. If a command times out or has an ambiguous response, inspect durable state before any retry.
9. Run all project-specific gates on the exact candidate, review privacy and signatures, then
   publish only with the authority granted by project policy.
10. Release stopped work as `done`, `open`, or `blocked`; reconcile and run `doctor --live`.

Never edit generated views, binding files, the vendor lock, or vendored runtime manually. A binding
failure means the wrong checkout, origin, product, runtime configuration, or current directory is in
use. Stop and correct that environment; do not bypass the check.

For SQLite projects, task Markdown is a projection: never use direct file edits as mutations. Use
`handoffctl` commands, then regenerate a damaged or stale projection with `handoffctl reconcile`.
For Git projects, the existing locked Markdown authority contract applies. Never copy a SQLite
database or backend selector between projects.

Do not store raw logs, prompts, transcripts, credentials, private paths, hostnames, or tokens in
public task records. Store concise conclusions, immutable commit/run identifiers, limitations and
the exact next action.

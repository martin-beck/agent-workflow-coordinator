# Git-backed command latency: AR-0120 contract and design

## Objective and measurement boundary

AR-0120 targets an end-to-end wall time no greater than 5% of the same
command's exact-revision baseline on a representative Git-backed state. A
success report must name the command, inputs, repository revisions, authority
backend, worktree/task count, worker count, observation freshness, and whether
the measured completion means locally durable, remotely published, or both.
It must include the wrapped subprocess and required network waits. Moving
work beyond the response is a contract change, not a speedup at the same
boundary.

The first read-only Agent Systems Benchmark reconnaissance on 2026-10-09 found
749 state tasks and 746 product worktrees. Its then-vendored development
Coordinator v0.3.57 took 1.02 seconds for static `doctor`, 25.04 seconds for
`doctor --live`, and 32.00 seconds for `snapshot` in single local samples.
Profiling the live doctor recorded about 3,004 subprocess invocations;
product scanning dominated while two GitHub observations alone took about
2.7 seconds. These numbers are diagnostic, not a paired current-release
baseline or a guarantee. The v0.3.59 source and a disposable copy of that
state, with controlled GitHub responses and push disabled, took 21.211 seconds
for one `reconcile` and 14.800 seconds for two simultaneous reconciles;
neither timed out. The local source state and product were observed only.
Repeat and pin this experiment before comparing an implementation.

`tests/git_command_latency_benchmark.py` provides the first reproducible
per-route comparison harness. It pins a supplied baseline source commit and
one captured ASB-state commit into separate disposable clones, overlays the
corresponding runtime, uses fixed local GitHub responses, disables state push,
and observes the source product with `GIT_OPTIONAL_LOCKS=0`. It reports
bounded process wall/CPU/block I/O and outcome counts. Repeat cases in
alternating baseline/candidate order; source worktree-listing changes are
reported so a moving inventory cannot silently count as acceptance evidence.
For example:

```sh
python tests/git_command_latency_benchmark.py \
  --state /path/to/asb-state --product /path/to/asb \
  --baseline EXACT_40_CHARACTER_COORDINATOR_COMMIT \
  --routes doctor,doctor-live,snapshot,render-check,reconcile \
  --counts 1,2 --repetitions 2
```

A smoke comparison of identical v0.3.59 runtime copies already ranged from
17.9 to 19.7 seconds for one `doctor --live`, 15.8 to 18.9 seconds for
`snapshot`, and 14.8 to 20.3 seconds for `reconcile` while all routes
succeeded. This is host/scheduling variability, not a candidate speedup.
One-sample or baseline-first numbers are insufficient for the 5% gate.

## Command inventory and lower bounds

| Git-backed route | Completion required by the current contract | Principal work and target caveat |
| --- | --- | --- |
| `snapshot`, `doctor --live` | Fresh product/GitHub observation, consistent validated authority view | Worktree inventory and remote queries dominate; an older cached observation cannot be reported as fresh. |
| `doctor`, `render-status --check` | Complete structural, privacy, and generated-view validation | Whole-repository privacy scan and full task graph/rendering; direct edits and missed invalidations must be detected. |
| `claim`, `heartbeat`, `update`, `release`, `promote`, `pause`, `resume`, `unblock`, `recover-expired`, `gate`, `checkpoint` | Fenced accepted transition, validated task/projections, signed durable Git commit, optional required replica publication | A signed commit and any required remote acknowledgement cannot be removed from the unchanged end-to-end boundary. |
| Git `roles` and `directive` operations | The route's exact durable read or mutation result | Preserve binding, revision, privacy, and precedence admission; measure each subcommand rather than hiding it in a family average. |
| `run` | Completed wrapped process, fsynced classified result, task commit, and post-command reconcile | A caller-selected command can run arbitrarily long; zero coordinator overhead cannot reduce its total time by 95%. |
| `reconcile` with/without `--commit`/`--push` | Applied live observation, optional signed commit, optional confirmed push | Separate local and remote completion; an outbox alone does not satisfy a synchronous `--push`. |
| `rollback`, `migrate`, `upgrade` subcommands, `init` | Their full one-off safety, equivalence, or initialization contract | Cold scans, backups, validation, signing, and external effects may dominate; do not silently exclude these from an “all commands” claim. |
| `board`, `metrics` | Unavailable for Git authority | Report as unsupported, not fast or slow. |

The literal all-command target cannot be proved for arbitrary `run` payloads,
unbounded remote latency, or an unchanged strict fresh-remote route: those
external operations have lower bounds independent of coordinator code. This
does not waive the target. Such a row remains unresolved until the user
explicitly accepts a separate completion contract; unchanged routes retain
their current behavior.

## Candidate architecture, not yet implemented

Keep the CLI as the mandatory project-bound route. A repository-common local
service could retain a generation-fenced task/dependency index and materialized
projection digests across CLI invocations. It would watch task, index, Git,
binding, policy, and product-worktree changes, invalidate only affected data,
and fall back to a full audit on missed events, external edits, changed inputs,
or restart ambiguity. Every acknowledged mutation still needs exact-owner and
revision fencing, local durability, and a signed commit at the declared
linearization point. Batching independent operations or staging a Git tree
directly is admissible only after process-death and independent-reader tests
establish equivalent semantics.

Product and GitHub observations could be coalesced and carried with explicit
source revision and `observed_at` values. A durable publication outbox could
free the authority lock while a remote push is pending, but its receipt must
say **locally committed, not remotely published** until the exact remote ref
is confirmed. A possible opt-in fast mode could expose those distinctions;
it must not replace the existing strict completion contract without an
explicit decision. Offline vendoring and normal SQLite operation must not
require a daemon or network connection.

## Qualification sequence

1. Build an exact-command, exact-revision benchmark on disposable state and
   product copies. Run the same inputs and fixed observation responses on
   baseline and candidate. Report request-to-local-durable and
   request-to-remote-published times separately, p50/p95/max, CPU/I/O, and
   failures at 1, 2, 4, 8, 16, 32, and 64 workers where valid.
2. Reproduce against the full Agent Systems Benchmark worktree inventory;
   exercise a one-worktree copy separately to isolate whole-state admission
   from product-scan cost. Include cold start, warm steady state, source edits,
   watcher overflow, GitHub slowness/offline, and replica divergence.
3. Differentially audit every outcome against a full, strict doctor and the
   exact task/session/command/commit/ref state. Prove rejected claims and stale
   revisions do not mutate authority; kill workers/service at every
   pre-commit and post-commit boundary; verify no false fresh or remote-success
   report.
4. Review the exact implementation tree, run native and formal gates, merge
   only after hosted success, verify the merge head, then publish a new release
   only if the release contract remains truthful. Any unchanged route still
   above its literal 5% target prevents an AR-0120 “20x complete” claim.

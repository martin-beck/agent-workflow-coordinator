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

The first implemented step observes independent product worktrees with a
bounded 32-worker pool. It preserves Git's inventory order, reads every
checkout freshly, and leaves the authoritative observation boundary intact.
With the 750-task/746-worktree ASB fixture pinned to state commit
`f41d59edfb1615117a3aff5bf965001486b509a5`, baseline
`c9c21332841b80c967b174893596df1f3326b30b` and candidate
`0149d0c0c72785f44b31ef548843e68c16be548e`, alternating two single-worker
samples gave these command wall times (seconds):

| Route | Baseline | Candidate | Candidate / paired baseline range |
| --- | ---: | ---: | ---: |
| `doctor --live` | 18.093, 17.147 | 1.929, 1.865 | 10.7%, 10.9% |
| `snapshot` | 15.025, 20.324 | 2.005, 1.889 | 13.3%, 9.3% |
| `reconcile` | 19.420, 20.952 | 2.076, 2.089 | 10.7%, 10.0% |

All 12 measured commands returned success. The source product HEAD and
worktree listing stayed stable; the source state HEAD moved because other
workers continued work, but both fixture clones used the same pinned commit.
The candidate uses roughly 10--13% of baseline wall time for these routes,
not the required 5%, and it still consumes roughly 14 CPU-seconds per live
command. This is a local first-stage result, not a concurrency, cold-start,
failure-mode, or all-command qualification. Independent checkpoint review
found that this early harness version did not pin candidate file bytes, capture
outputs, or detect dirty-worktree/ref changes; the harness now clones both
exact runtime commits, hashes outputs, and fingerprints source checkout status
and refs. The earlier timings remain diagnostic only until re-run with those
repairs and a stable input fixture.

The follow-up 16-process probe (`tests/git_mixed_command_probe.py`) uses one
disposable Git state clone plus 16 synthetic, dependency-free task/spec pairs;
its live product inventory is the same 746-worktree ASB repository, observed
read-only. On candidate `1d306b02adbde5988306e5bc48c3f78b5d809cd1`,
16 simultaneous claims and 16 simultaneous releases on distinct tasks all
succeeded, with batch walls of 6.1--7.7 seconds across repeated local runs.
Read-only and live/reconcile mixtures also passed static and live `doctor`
after each batch. Four `board` and four `metrics` invocations rejected as
unsupported on Git authority, and four unassigned `roles check` calls
rejected, as expected; four `directive list` calls succeeded. Separate
16-lane directive create and activation batches also succeeded, with
roughly 0.09-second maximum exclusive lock holds. Role assignment/removal
CAS batches admitted exactly one request and rejected 15 stale revisions,
while the role reads succeeded; the fixture must seed roles for any
pre-existing active tasks before initializing role admission.

A 16-process mixture of four heartbeats, four updates, four `run` invocations
wrapping `/usr/bin/true`, and four reconciles **did not pass**: one run had
two mutation lock timeouts, and a repeat had one heartbeat lock timeout.
The repeat's private phase trace showed 16 claims with maximum lock wait
7.067 s and hold 0.520 s; in the mixed batch, `git_mutate` reached 10.000 s
wait and 1.224 s hold, while `git_reconcile` reached 8.279 s wait and
1.272 s hold. The successful calls and post-batch static/live doctors show
durable consistency, but do not turn timed-out calls into success. The
remaining 0.5--1.3 s serialized Git mutation/reconciliation phases are a
concrete scalability bottleneck under the current 10 s admission deadline.

The new 16-lane adversarial fixture races 15 rejected wrong-owner,
stale-revision, and malformed-gate calls with one valid owner update on the
same task. At pinned ASB state `d4bc028efae43d53e5a9868c40eec41426f7824f`,
the valid update completed in 1.389 seconds; all 15 invalid attempts failed,
exactly one Git commit was added, the task revision advanced once, the owner
remained unchanged, and static/live `doctor` both passed. This is a concrete
integrity and liveness witness for that adversarial mix, not a proof against
arbitrary malicious workloads. In the same run, the separate four-each
heartbeat/update/`run`/reconcile batch had three lock timeouts and 13
successes; post-batch doctors remained green. The source ASB state head moved
during the run due to other workers, though the probe itself used a pinned
disposable clone; the observed product inputs did not change.

This matrix covers a meaningful subset, **not all commands**. `checkpoint`,
`gate`, `promote`, `pause`/`resume`, `recover-expired`, `unblock`, Git
`migrate`/`upgrade`/`rollback`, `init`, and replica-push/failure paths still
require route-specific fixtures and differential review. The trace is a
diagnostic on one host, not a statistical p95 or a 5% acceptance result.

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

## Further candidate architecture, not yet implemented

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

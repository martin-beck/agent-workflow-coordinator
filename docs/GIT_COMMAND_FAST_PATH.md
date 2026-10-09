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

Independent review found that the initial adversarial check would accept any
nonzero hostile exit, including an unrelated lock timeout, and did not inspect
the good worker's actual note. The repaired check requires each of the 15
specific authority/validation rejection classes plus exactly one recorded
good update. A targeted rerun at pinned ASB state
`75d4feb27a1970d3d8d136a283027ec9e153b55a` passed: the good update
finished in 1.056 seconds, all 15 expected rejection classes matched, exactly
one commit and task-revision increment occurred, ownership was retained,
static/live doctors were green, and both source state head and product input
fingerprint remained stable. The reviewer also found two latency-harness
blind spots; output fingerprints now preserve multiplicity, and the source
product fingerprint includes tracked dirty diffs and untracked file bytes.
These repaired harness assertions still need independent re-review and paired
candidate/baseline reruns.

A paired one-worker rerun on pinned ASB state
`654f3195f0fe7e9a367561783960629fd451ace3` and product inputs stable
throughout measured 12.658/1.454 seconds for baseline/candidate live doctor,
15.567/1.370 seconds for snapshot, and 19.170/1.568 seconds for reconcile.
That is roughly 11.5%, 8.8%, and 8.2% of baseline respectively, still above
the 5% gate. Outputs and outcomes agreed for live doctor and reconcile. Raw
snapshot hashes differed even though both fixture trees and CURRENT views were
identical: each disposable fixture has its own signed state commit, and
`snapshot` prints that commit on its first line. The harness now reports a
separate body hash after verifying that exact-commit prefix; a follow-up
snapshot-only rerun again found equal fixture trees and CURRENT views, with
equal verified snapshot body hashes but different raw commit-bearing hashes.
This is not an excuse to ignore any other output or
side-effect difference.

A second independent review confirmed the earlier three benchmark/adversarial
repairs but found three narrower evidence gaps. The adversarial `run` requests
now wrap a command that would write a marker in the disposable fixture and
require its absence; liveness additionally requires the good worker to finish
within 10 seconds. On pinned state
`654f3195f0fe7e9a367561783960629fd451ace3`, the stronger 16-way test
passed: all 15 expected rejection classes, one good update in 0.899 seconds,
one commit and revision advance, no unauthorized subprocess marker, green
static/live doctors, and unchanged source state/product fingerprints. The
snapshot harness now verifies the printed first-line commit against the
fixture's exact HEAD before comparing bodies, and the product fingerprint
also hashes staged index diffs. Those final repairs still require exact-head
independent review and a post-repair benchmark run.

The first post-repair snapshot rerun was **invalid**, not a performance result:
the ASB source state head, product worktree listing, and product input
fingerprint moved while it ran; the candidate snapshot correctly failed
closed with a stale/changed observation. The harness now returns a failing
exit status for unequal outcomes or detected source/fixture drift instead of
leaving an invalid JSON result looking like a successful qualification.
A repeat on a newer 747-worktree ASB observation did produce equal successful
snapshot bodies and state trees, but the source product fingerprint and state
head changed during that run. The harness exited nonzero as intended; its
13.483/1.423-second timings remain diagnostic, not a qualified pair. A
frozen, representative product fixture or quiet source interval is needed for
post-repair performance qualification.

`tests/build_frozen_git_fixture.py` now constructs that disposable fixture
from a read-only ASB state/product pair. It mirrors both Git object stores,
pins the state commit, remote `main`, and each listed product worktree commit,
then checks out each selected worktree in a fresh tree. It keeps canonical
origin URLs for coordinator binding checks, while a fixture-local Git wrapper
serves only the pinned product `ls-remote origin refs/heads/main` observation
from the local mirror. Ignored build output and source dirty/untracked content
are **not** copied, so this represents the complete clean tracked-worktree
topology, not an exact byte copy of every active source checkout. Build it
with an empty temporary output directory and sufficient free space:

```sh
python tests/build_frozen_git_fixture.py \
  --source-state /path/to/asb-state --source-product /path/to/asb \
  --output /tmp/your-empty-disposable-fixture
```

The 2026-10-09 fixture pinned ASB state
`65ce1b74e39f06c22ec2dfdce706713793a3d132`, product primary
`a9abcf2e63f761e314593e9abc6bf074b7418e5e`, and 750 product worktrees
with 751 state tasks. The benchmark normalizes observed product metadata
*once* before cloning its baseline/candidate states; otherwise reconcile
time-stamps changed worktree observations at different setup times. It
compares committed, staged, dirty, and untracked domain-state bytes while
excluding only the exact files deliberately overlaid from the two runtimes;
ignored `.runtime` authority state is included except ephemeral lock bytes
and the wall-clock `at` value of the last-reconcile marker. It also compares
Git history depth and requires each measured command to extend its starting
HEAD without rewriting it. An exact overlay-byte fingerprint must stay
unchanged within each command, as must every non-HEAD Git ref. Output,
outcomes, and the generated current
view are compared, and both source checkout inputs are fingerprinted before
and after a run (including raw ignored runtime files in the source state). An earlier
static `doctor` pair was 0.987 s baseline versus 1.014 s candidate; this
route has no material speedup, and that run predates the stronger fingerprint
checks, so it is diagnostic rather than final qualification. A final exact-patch
static `doctor` pair passed all output, domain-byte, history, ref, overlay,
and source-stability checks at 1.033 s baseline versus 1.045 s candidate;
the 5% gate is not met.

On the same frozen fixture, `tests/git_mixed_command_probe.py
--only-adversarial` admitted all 16 disjoint claims without lock timeout
(8.006 s batch wall, 7.368 s maximum lock wait). Then 15 wrong-owner,
stale-revision, and malformed-gate invocations raced one legitimate update.
All 15 failed with their specific expected rejection class; the good update
finished in 1.285 s, exactly one commit and task-revision advance occurred,
the note appeared once, ownership was retained, and the unauthorized `run`
subprocess marker was absent. Static/live `doctor` passed; the source product
input fingerprint and state HEAD stayed unchanged. Independent review then
found that the probe did not inspect uncommitted effects elsewhere or dirty
source-state drift. The repaired probe checks both and requires committed
changes to stay within the good worker's task, session, and generated views.
The stronger rerun on the same frozen fixture passed: 16/16 claims succeeded
without timeout; the good update completed in 1.007 s among 15 specifically
rejected requests, made exactly one commit and revision advance, left no
unexpected committed, uncommitted, or ignored-runtime changes, and recorded
exactly one session record matching the resulting task snapshot and its note once.
Ownership, static/live doctor, source-state/product fingerprints, and the
unauthorized-subprocess check all passed. The fixture builder now rejects
output paths overlapping any linked state or product worktree. This latest
repaired result also checks that non-HEAD refs and the starting Git ancestry
are preserved. Independent review found and drove repairs for several
false-pass paths and confirmed the final ancestry guard by inspection; the
final exact-patch adversarial rerun passed on the same 750-worktree fixture:
16/16 claims, all 15 expected hostile rejections, one good update in 0.662 s,
one exact matching session record, one commit extending the starting HEAD,
no non-HEAD ref or runtime changes, and green static/live doctors.

Final exact-head review found the mixed-command probe still printed source
drift without failing. Both its targeted adversarial and complete-route paths
now exit nonzero if either the observed source state head or product inputs
change. This keeps a successful probe exit from implying qualification on a
moving source.

On 2026-10-09, a repeat on the frozen 750-worktree fixture initially could not
start because an active claim captured in the fixture had expired. The probe
extends only parseable, already-expired active claim leases in its disposable
state clone before the setup reconciliation commits the fixture; malformed
claim expiries still reject the source fixture. It reports how many claims
were stabilized and never changes the source fixture or live ASB repositories.
With two fixture claims stabilized and Coordinator `712b36e`, 16/16 disjoint
claims and the 15-rejected/one-good adversarial batch passed. The next
four-each heartbeat/update/`run`/reconcile batch had **eight** lock timeouts
(2 heartbeat, 3 update, 3 run), while all four reconciles succeeded. The
largest observed exclusive holds were 3.752 s for `git_mutate` and 2.469 s
for `git_reconcile`; post-batch static and live doctors passed. This repeat
confirms a correctness-preserving but still severe accepted-worker liveness
failure under the unchanged ten-second lock deadline.
At exact merged Coordinator `5249a62`, a separate 16-worker batch for the new
`accept` route passed 16/16 after 16/16 claims on the same frozen fixture.
Its maximum exclusive hold was 0.426 s, post-batch static/live doctors passed,
and the observed source state and product bytes stayed unchanged. A stronger
rerun verified all 16 exact acceptance records, 16 extending SSH-signed/DCO
commits, unchanged unrelated task fields and bodies, no unrelated committed or
uncommitted paths, stable ignored runtime state and non-HEAD refs, and green
doctors. This tests the new route in one contention pattern, not the full
command matrix.

A subsequent single-task-snapshot mutation experiment was rejected after
independent review: although it preserved lock-respecting writers, it could
miss a concurrent out-of-band edit to another task that the existing
post-write full scan would catch. It also did not resolve 16-way contention:
one diagnostic ASB run still had two mutation lock timeouts, and the source
product moved while the run was in progress. The experimental runtime change
was reverted; the full post-write task validation remains. The 16-way probe
now also fails on any unexpected route-success count or failed post-batch
static/live doctor instead of merely printing those failures.

This matrix covers a meaningful subset, **not all commands**. `checkpoint`,
`gate`, `promote`, `pause`/`resume`, `recover-expired`, `unblock`, Git
`migrate`/`upgrade`/`rollback`, `init`, and replica-push/failure paths still
require route-specific fixtures and differential review. The trace is a
diagnostic on one host, not a statistical p95 or a 5% acceptance result.

### Partial projection and privacy-scan checkpoint

The signed AR-0120 checkpoint ending at `79b2f9b` avoids fsyncing unchanged,
ordinary generated task views, reuses the rendered view during post-write
validation only when the freshly reloaded tasks and project settings are
identical, and applies conservative literal prefilters before the unchanged
privacy regexes. It retains the full post-write task scan, fresh projection
content checks, signed Git commits, and strict command completion semantics.
Race tests cover a replaced projection pathname, out-of-band sibling task and
profile edits, wrong-mode and linked views, and rollback. An independent
exact-head review found no actionable P1/P2 defect. The native suite passed
1,734 tests and the combined branch-coverage gate at 95%; the CCN-14,
formatting, lint, typing, and strict vendor privacy-closure gates passed.

On the frozen 750-worktree ASB fixture, an unprofiled full privacy pass took
0.812/0.809 seconds without prefilters and 0.317/0.312 seconds with them,
returning the same finding list. A single cProfiled, signed Git update took
0.625 seconds before the view/unchanged-content reuse and 0.486 seconds after;
these are exploratory individual samples, not a paired p95 result. The
`02f5b45` 16-worker probe passed all 16 disjoint claims in 6.171 seconds and
the 15-rejected/one-good adversarial batch in 1.189 seconds, with exact
integrity checks green. Its four-each heartbeat/update/`run`/reconcile batch
still had **six lock timeouts**, despite green post-batch static/live doctors;
maximum measured exclusive holds were 3.284 seconds for `git_mutate` and
2.182 seconds for `git_reconcile`. A prior `4eba510` run also had six mixed
timeouts. The final `79b2f9b` refactor restored the complexity gate. An
exact-head rerun with the same runtime at `e7cdaa1` passed 16/16 claims in
5.885 seconds and the adversarial batch in 1.091 seconds, but the accepted
mixed batch still had **seven lock timeouts**. Its largest `git_mutate` hold
was 3.065 seconds and `git_reconcile` hold was 1.177 seconds; static/live
doctors remained green. These results do not meet the accepted-worker
liveness or per-command 5% targets, and no opt-in receipt runtime is
implemented yet. The checkpoint is a partial optimization only.

## Command inventory and lower bounds

| Git-backed route | Completion required by the current contract | Principal work and target caveat |
| --- | --- | --- |
| `snapshot`, `doctor --live` | Fresh product/GitHub observation, consistent validated authority view | Worktree inventory and remote queries dominate; an older cached observation cannot be reported as fresh. |
| `doctor`, `render-status --check` | Complete structural, privacy, and generated-view validation | Whole-repository privacy scan and full task graph/rendering; direct edits and missed invalidations must be detected. |
| `claim`, `heartbeat`, `update`, `accept`, `release`, `promote`, `pause`, `resume`, `unblock`, `recover-expired`, `gate`, `checkpoint` | Fenced accepted transition, validated task/projections, signed durable Git commit, optional required replica publication | A signed commit and any required remote acknowledgement cannot be removed from the unchanged end-to-end boundary. |
| Git `roles` and `directive` operations | The route's exact durable read or mutation result | Preserve binding, revision, privacy, and precedence admission; measure each subcommand rather than hiding it in a family average. |
| `run` | Completed wrapped process, fsynced classified result, task commit, and post-command reconcile | A caller-selected command can run arbitrarily long; zero coordinator overhead cannot reduce its total time by 95%. |
| `reconcile` with/without `--commit`/`--push` | Applied live observation, optional signed commit, optional confirmed push | Separate local and remote completion; an outbox alone does not satisfy a synchronous `--push`. |
| `rollback`, `migrate`, `upgrade` subcommands, `init` | Their full one-off safety, equivalence, or initialization contract | Cold scans, backups, validation, signing, and external effects may dominate; do not silently exclude these from an “all commands” claim. |
| `board`, `metrics` | Unavailable for Git authority | Report as unsupported, not fast or slow. |

The literal all-command target cannot be proved for arbitrary `run` payloads,
unbounded remote latency, or an unchanged strict fresh-remote route: those
external operations have lower bounds independent of coordinator code. The
user selected an **opt-in fast completion contract** on 2026-10-09. The
unchanged strict routes retain their current behavior and are not reported as
meeting the 5% target. A fast-mode timing must name which receipt boundary it
measures; comparing a queued receipt to a strict completed response without
labelling the changed contract is invalid.

The opt-in receipt sequence is `queued-local` (fsynced intent only, no
authority transition), `running`, `completed-local` (fenced, validated
transition and a verified signed Git commit), then `published-remote`
(an observed exact remote-main ref that contains that commit). Terminal
`rejected` and `ambiguous` phases are separate from success. A receipt
exposes the exact phase and failure classification; neither a queued intent
nor an uncertain push is reported as completed.

The current prototype implements these phases only for typed Git
`heartbeat` intents. It binds a private WAL queue to one project and an
idempotency key and typed-input digest; same-key different-input retries
reject. A single service lease fences execution and restart recovery. The
internal worker verifies the signed, DCO-matching, marked task commit before
recording local completion, and records remote publication only after
observing a ref containing that commit. A real disposable signed-Git and bare
remote test covers the local-to-remote path. The strict heartbeat CLI retains
its old owner-only form and accepts an optional exact-revision fence.

The preliminary user-facing route is
`handoffctl fast heartbeat TASK --owner OWNER --expected-revision N --key KEY`.
It returns a durable `queued-local` receipt and does not claim a heartbeat
has occurred. `handoffctl fast receipt RECEIPT_ID` reads its current phase;
`handoffctl fast worker --limit N` performs one explicitly invoked batch,
including local commit verification and a separate remote observation attempt.
`handoffctl fast worker --serve` runs a resident local executor under one
repository-common service lock. `handoffctl fast publisher --serve` uses a
different lock for remote observation and publication, so a slow push does not
hold the local executor's service lock. These are manually started processes;
there is no service supervisor, wake-up mechanism, or broad per-command
latency qualification yet. The worker handles no command other than heartbeat.
Arbitrary
`run` payloads cannot be retried after ambiguous execution without risking
double effects; they require a distinct design. This prototype is not 5%
evidence, a general all-command fast path, or release qualification. The
strict commands remain the compatibility path.

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

# Git backend contention investigation

## Reproduction

Run `python tests/git_contention_benchmark.py` from a development checkout.
It creates disposable, project-bound Git state and product repositories, signs
fixture commits, gives each worker its own active AR and owner, then invokes the
real `tools/handoffctl.py run` CLI in separate processes. The product observation
uses a controlled local `gh` response with a 0.6-second delay per query; no
GitHub state is changed. The script compares pinned pre-change commit
`113dc61029f0e0c57bc7832e1e41430eafa17e73` with the candidate and prints
bounded aggregate results only.

An observed run on 2026-10-08, with two delayed GitHub queries per
reconciliation, produced:

| Workers | Baseline batch wall | Candidate batch wall | Baseline lock timeouts | Candidate lock timeouts |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 1.60 s | 1.60 s | 0 | 0 |
| 2 | 3.04 s | 1.69 s | 0 | 0 |
| 4 | 5.95 s | 1.92 s | 0 | 0 |
| 8 | 11.59 s | 2.27 s | 0 | 0 |
| 16 | 17.58 s | 2.94 s | 4 | 0 |

At 16 workers, the candidate's opt-in trace recorded a maximum 114 ms
`git_reconcile` lock hold, 76 ms `git_mutate` hold, 17 ms `run_preflight`
shared hold, and 953 ms acquisition wait. All 16 routes completed. The
baseline runtime has no phase trace; its aggregate timings and timeout count
are measured through the same CLI workload, not inferred from a mocked lock.
This local fixture is a controlled contention test, not a production throughput
claim or a substitute for a real project-scale probe.

After the checkout-local ticket and retry repair, a separate 16-worker repeat
completed in 4.25 seconds with zero lock timeouts; its longest recorded lock
hold was 104 ms. Retries spend additional time scanning outside the lock, so
this repeat is slower than the initial candidate sample but still avoids the
baseline lock convoy.

## Project-scale cross-check

`tests/git_scale_probe.py` clones a supplied product/state pair into temporary
local repositories. It disables push, uses fixed local GitHub responses, and
can observe the source product inventory with `GIT_OPTIONAL_LOCKS=0`; all
coordinator writes remain in the disposable state clone. Against Agent Systems
Benchmark on 2026-10-08, the state held 746 tasks and the product advertised
731 worktrees. The released ASB vendor embeds project-specific task evidence
classes. For the current upstream candidate, the disposable clone alone was
given the equivalent tracked policy and complete current vendor allowlist;
without those compatibility inputs, current upstream correctly rejects the
older state snapshot.

| Reconciles against 731 worktrees | Baseline Git backend | Candidate |
| --- | ---: | ---: |
| One worker, batch wall | 26.79 s | 14.14 s |
| Two workers, batch wall | 24.97 s | 12.23 s |
| Two-worker lock timeouts | 1 | 0 |

The candidate's longest recorded authority-lock hold was below 0.9 seconds
in the final two-worker sample; the longest wait was 0.85 seconds. The
porcelain head/branch reuse removed approximately 1,462 per-worktree Git
subprocesses per scan. These are local host measurements with warm caches and
one controlled run, not a service-level latency guarantee. In particular,
each worker still performs its own potentially expensive external inventory.

A separate candidate stress kept the copied 746-task state but observed its
one-worktree product clone. At 16 independent reconcile processes, all 16
completed in 5.08 seconds without lock timeouts; maximum acquisition wait was
4.31 seconds and maximum hold was 0.86 seconds. This exercises whole-state
validation and scan-ticket retries without multiplying the 731-worktree
source scan across 16 processes.

## Cause and repair

Before this change, every Git `run` reconciled under an exclusive
repository-common lock, including product worktree inventory, two GitHub
queries, and remote-main observation. `snapshot` likewise performed its live
scan under a shared lock. A few workers therefore formed a convoy whose wait
could exceed the ten-second lock deadline even when the wrapped commands were
trivial.

External observations now run outside the authority lock. Applying observations
to task records, rendering and validating projections, committing, and optional
replication remain serialized. A short, separate, checkout-local scan ticket
prevents an older, slower scan from overwriting a newer published observation
in the same checkout; overtaken scans retry outside the authority lock.
The runtime configuration and permanent binding are rechecked before applying
the scan. A commit/push caller commits only pending coordinator changes whose
recorded content still matches and then replicates; an edited pending file
fails closed rather than being staged. A failed push cannot undo the locally
published scan ticket. The shared lock in
`snapshot` covers validation and reading the authoritative view, not the
product/GitHub observation; it retries if another reconciliation publishes
between its scan and locked validation.

The ticket is advisory ordering metadata, not task authority. Abandoned tickets
leave harmless gaps; a failed publication marker can be repaired by a fresh
scan in the same checkout. A failed external scan
does not acquire the authority lock. Unit tests cover out-of-order scans,
changed bindings, lock-free observation, checkout-local ticket uniqueness,
lock timeout tracing, mixed commit/plain reconciliation, pending-content drift,
failed push, and snapshot publication races.

## Remaining scaling boundary

Git mutation and reconciliation still serialize commits, task/projection
validation, and any enabled replica fetch/push. This repair does not claim that
network replication under the authority lock is scalable. Moving it out safely
requires a durable publication outbox or an equivalent exact-head protocol so
remote ambiguity and concurrent local commits remain fenced. Large product
worktree inventories also make each external scan expensive even though they no
longer block unrelated authority mutations; coalescing those scans needs an
explicit freshness policy and strict `doctor --live` bypass.

`HANDOFFCTL_LOCK_TRACE` is an opt-in path for a private local JSONL file. Each
line contains only a fixed phase label, shared/exclusive mode, duration,
timeout flag, and monotonic timestamp. Do not check trace files into a state
repository or treat their absence as proof of no contention.

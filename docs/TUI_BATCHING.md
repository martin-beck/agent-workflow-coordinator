# Autonomous progress before TUI escalation

`tools/decision_batch_policy.py` is the Coordinator-side planning boundary for
human decision sessions. An autonomous worker supplies revision-bound AR
snapshots and receives two outputs:

- `autonomous_ar_ids`: ready ARs with no human dependency that may continue;
- `human_batches`: explicit, deterministic groups of AR decision requests to
  present through the Guidance/TUI bridge.

The planner never infers a batch relationship. Requests without the same
explicit `batch_key` become separate sessions. Grouping also does not authorize
responses across points: AWG still validates each decision request and the TUI
returns independent revision-bound outcomes.

The worker should run the autonomous IDs first, then open the next human batch.
An AR becomes eligible for a batch only when Guidance has created the required
`human_interaction` trigger and Coordinator has validated its task revision.

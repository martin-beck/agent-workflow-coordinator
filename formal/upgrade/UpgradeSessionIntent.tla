----------------------------- MODULE UpgradeSessionIntent -----------------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS FiniteSets, Naturals, TLC

(***************************************************************************)
(* Bounded session-only intent contract.                                    *)
(*                                                                           *)
(* This model is diagnostic evidence for the durable control-store intent   *)
(* protocol.  It does not model SQLite VFS/WAL fsync, Python refinement,   *)
(* authority writes, or a production recovery implementation.               *)
(***************************************************************************)

CONSTANTS Processes, NoProcess, MaxRevision

Statuses == {"held", "releasing", "released", "ambiguous"}
Intents == {"none", "prepared", "committed", "ambiguous"}
NoOwner == NoProcess

VARIABLES status, intent, revision, expectedRevision, proposedRevision,
          owner, alive, lockOwner, fence, writes

vars == <<status, intent, revision, expectedRevision, proposedRevision,
          owner, alive, lockOwner, fence, writes>>

Init ==
    /\ status = "held"
    /\ intent = "none"
    /\ revision = 1
    /\ expectedRevision = 0
    /\ proposedRevision = 0
    /\ owner = NoOwner
    /\ alive = [p \in Processes |-> TRUE]
    /\ lockOwner = NoOwner
    /\ fence = 0
    /\ writes = 0

Prepare(p) ==
    /\ alive[p]
    /\ lockOwner = NoOwner
    /\ status = "held"
    /\ intent = "none"
    /\ lockOwner' = p
    /\ owner' = p
    /\ expectedRevision' = revision
    /\ proposedRevision' = revision + 1
    /\ intent' = "prepared"
    /\ UNCHANGED <<status, revision, alive, fence, writes>>

CommitSession(p) ==
    /\ alive[p]
    /\ lockOwner = p
    /\ owner = p
    /\ intent = "prepared"
    /\ status = "held"
    /\ revision' = proposedRevision
    /\ status' = "releasing"
    /\ UNCHANGED <<intent, expectedRevision, proposedRevision,
                  owner, alive, lockOwner, fence, writes>>

PublishIntent(p) ==
    /\ alive[p]
    /\ lockOwner = p
    /\ owner = p
    /\ intent = "prepared"
    /\ status = "releasing"
    /\ intent' = "committed"
    /\ UNCHANGED <<status, revision, expectedRevision, proposedRevision,
                  owner, alive, lockOwner, fence, writes>>

Release(p) ==
    /\ alive[p]
    /\ lockOwner = p
    /\ owner = p
    /\ intent = "committed"
    /\ status = "releasing"
    /\ status' = "released"
    /\ intent' = "none"
    /\ lockOwner' = NoOwner
    /\ owner' = NoOwner
    /\ UNCHANGED <<revision, expectedRevision, proposedRevision,
                  alive, fence, writes>>

Crash(p) ==
    /\ alive[p]
    /\ lockOwner = p
    /\ owner = p
    /\ intent \in {"prepared", "committed"}
    /\ alive' = [alive EXCEPT ![p] = FALSE]
    /\ lockOwner' = NoOwner
    /\ owner' = NoOwner
    /\ UNCHANGED <<status, intent, revision, expectedRevision,
                  proposedRevision, fence, writes>>

RecoverCommitted(p) ==
    /\ alive[p]
    /\ lockOwner = NoOwner
    /\ owner = NoOwner
    /\ intent = "committed"
    /\ status = "releasing"
    /\ lockOwner' = p
    /\ owner' = p
    /\ UNCHANGED <<status, intent, revision, expectedRevision,
                  proposedRevision, alive, fence, writes>>

RecoverUnknown(p) ==
    /\ alive[p]
    /\ lockOwner = NoOwner
    /\ intent = "prepared"
    /\ status \in {"held", "releasing"}
    /\ status' = "ambiguous"
    /\ intent' = "ambiguous"
    /\ revision' = revision + 1
    /\ fence' = fence + 1
    /\ lockOwner' = p
    /\ owner' = p
    /\ UNCHANGED <<expectedRevision, proposedRevision, alive, writes>>

Reconcile(p) ==
    /\ alive[p]
    /\ lockOwner = p
    /\ owner = p
    /\ status = "ambiguous"
    /\ intent = "ambiguous"
    /\ revision < MaxRevision
    /\ status' = "held"
    /\ intent' = "none"
    /\ revision' = revision + 1
    /\ fence' = fence + 1
    /\ expectedRevision' = revision
    /\ proposedRevision' = revision + 1
    /\ UNCHANGED <<owner, alive, lockOwner, writes>>

AdmitWrite(p) ==
    /\ alive[p]
    /\ lockOwner = p
    /\ owner = p
    /\ status = "released"
    /\ intent = "none"
    /\ writes' = writes + 1
    /\ UNCHANGED <<status, intent, revision, expectedRevision,
                  proposedRevision, owner, alive, lockOwner, fence>>

Next ==
    \/ (\E p \in Processes:
          Prepare(p) \/ CommitSession(p) \/ PublishIntent(p) \/ Release(p)
          \/ Crash(p) \/ RecoverCommitted(p) \/ RecoverUnknown(p)
          \/ Reconcile(p) \/ AdmitWrite(p))
    \/ UNCHANGED vars

TypeOK ==
    /\ status \in Statuses
    /\ intent \in Intents
    /\ revision \in 1..MaxRevision
    /\ expectedRevision \in 0..MaxRevision
    /\ proposedRevision \in 0..MaxRevision
    /\ owner \in Processes \cup {NoOwner}
    /\ alive \in [Processes -> BOOLEAN]
    /\ lockOwner \in Processes \cup {NoOwner}
    /\ fence \in 0..MaxRevision
    /\ writes \in 0..MaxRevision

IntentRevisionBound ==
    intent = "none" \/ proposedRevision = expectedRevision + 1

IntentStatusCoherence ==
    /\ intent = "none" => status \in {"held", "released"}
    /\ intent = "committed" => status = "releasing"
    /\ intent = "ambiguous" => status = "ambiguous"

LockOwnership ==
    /\ lockOwner = NoOwner <=> owner = NoOwner
    /\ lockOwner # NoOwner => lockOwner = owner

NoWriteBeforeRecovery ==
    intent # "none" => writes = 0

AmbiguousIsWriteClosed ==
    status = "ambiguous" => intent = "ambiguous"

ReconcileRequiresFence ==
    intent = "none" /\ status = "held" /\ revision > 1 => fence > 0

Spec == Init /\ [][Next]_vars

THEOREM Spec => []TypeOK
THEOREM Spec => []IntentRevisionBound
THEOREM Spec => []IntentStatusCoherence
THEOREM Spec => []LockOwnership
THEOREM Spec => []NoWriteBeforeRecovery
THEOREM Spec => []AmbiguousIsWriteClosed
THEOREM Spec => []ReconcileRequiresFence

=========================================================================================

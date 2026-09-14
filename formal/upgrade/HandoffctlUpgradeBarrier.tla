-------------------------- MODULE HandoffctlUpgradeBarrier --------------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS FiniteSets, Naturals, TLC

(***************************************************************************)
(* Bounded v10 barrier/control-store contract.                          *)
(*                                                                         *)
(* This model describes the accepted control-plane boundary only.  It does *)
(* not model SQLite's VFS, WAL fsync, power loss, Python refinement, or the *)
(* coordinator authority itself.  A writer observes the durable barrier at *)
(* the abstract linearization point; unsafe states reject the write.       *)
(***************************************************************************)

CONSTANTS Processes, NoProcess, NoChild, MaxRevision

Statuses == {"absent", "held", "releasing", "released", "ambiguous"}
UnsafeStatuses == {"held", "releasing", "ambiguous"}
WriterPhases == {"idle", "requested", "accepted", "rejected", "done"}
Targets == {"new", "rollback"}
NoTarget == "none"
TerminalResults == {"none", "new", "rollback"}

VARIABLES
    sessionStatus,
    activeAttempt,
    attemptGeneration,
    fence,
    controlRevision,
    forwardChild,
    rollbackChild,
    forwardFailed,
    terminalTarget,
    terminalVerified,
    freshRuntimeVerified,
    writerPhase,
    writerAcceptedStatus,
    writerMutations,
    writerTarget,
    lockOwner

vars == <<sessionStatus, activeAttempt, attemptGeneration, fence,
          controlRevision, forwardChild, rollbackChild, forwardFailed,
          terminalTarget, terminalVerified, freshRuntimeVerified,
          writerPhase, writerAcceptedStatus, writerMutations, writerTarget,
          lockOwner>>

ForwardId(p) ==
    CASE p = "p1" -> "forward-p1"
      [] p = "p2" -> "forward-p2"

RollbackId(p) ==
    CASE p = "p1" -> "rollback-p1"
      [] p = "p2" -> "rollback-p2"

Init ==
    /\ sessionStatus = "absent"
    /\ activeAttempt = NoProcess
    /\ attemptGeneration = 0
    /\ fence = 0
    /\ controlRevision = 0
    /\ forwardChild = NoChild
    /\ rollbackChild = NoChild
    /\ forwardFailed = FALSE
    /\ terminalTarget = NoTarget
    /\ terminalVerified = FALSE
    /\ freshRuntimeVerified = FALSE
    /\ writerPhase = [p \in Processes |-> "idle"]
    /\ writerAcceptedStatus = [p \in Processes |-> "none"]
    /\ writerMutations = 0
    /\ writerTarget = [p \in Processes |-> "authority"]
    /\ lockOwner = NoProcess

Acquire(p) ==
    /\ sessionStatus \in {"absent", "released"}
    /\ attemptGeneration < MaxRevision
    /\ activeAttempt' = p
    /\ attemptGeneration' = attemptGeneration + 1
    /\ fence' = fence + 1
    /\ controlRevision' = 1
    /\ sessionStatus' = "held"
    /\ forwardChild' = NoChild
    /\ rollbackChild' = NoChild
    /\ forwardFailed' = FALSE
    /\ terminalTarget' = NoTarget
    /\ terminalVerified' = FALSE
    /\ freshRuntimeVerified' = FALSE
    /\ UNCHANGED <<writerPhase, writerAcceptedStatus, writerMutations,
                  writerTarget, lockOwner>>

BindForward(p) ==
    /\ sessionStatus = "held"
    /\ activeAttempt = p
    /\ forwardChild = NoChild
    /\ controlRevision < MaxRevision
    /\ forwardChild' = ForwardId(p)
    /\ controlRevision' = controlRevision + 1
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  rollbackChild, forwardFailed, terminalTarget,
                  terminalVerified, freshRuntimeVerified, writerPhase,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

ForwardFailure(p) ==
    /\ sessionStatus = "held"
    /\ activeAttempt = p
    /\ forwardChild # NoChild
    /\ forwardFailed = FALSE
    /\ forwardFailed' = TRUE
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  controlRevision, forwardChild, rollbackChild,
                  terminalTarget, terminalVerified, freshRuntimeVerified,
                  writerPhase, writerAcceptedStatus, writerMutations,
                  writerTarget, lockOwner>>

BindRollback(p) ==
    /\ sessionStatus = "held"
    /\ activeAttempt = p
    /\ forwardFailed
    /\ forwardChild # NoChild
    /\ rollbackChild = NoChild
    /\ controlRevision < MaxRevision
    /\ rollbackChild' = RollbackId(p)
    /\ controlRevision' = controlRevision + 1
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  forwardChild, forwardFailed, terminalTarget,
                  terminalVerified, freshRuntimeVerified, writerPhase,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

VerifyTerminal(p, target) ==
    /\ sessionStatus = "held"
    /\ activeAttempt = p
    /\ target \in Targets
    /\ IF target = "new"
          THEN forwardChild # NoChild
          ELSE rollbackChild # NoChild
    /\ terminalTarget' = target
    /\ terminalVerified' = TRUE
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  controlRevision, forwardChild, rollbackChild, forwardFailed,
                  freshRuntimeVerified, writerPhase, writerAcceptedStatus,
                  writerMutations, writerTarget, lockOwner>>

BeginReopen(p) ==
    /\ sessionStatus = "held"
    /\ activeAttempt = p
    /\ terminalVerified
    /\ terminalTarget \in Targets
    /\ controlRevision < MaxRevision
    /\ sessionStatus' = "releasing"
    /\ controlRevision' = controlRevision + 1
    /\ UNCHANGED <<activeAttempt, attemptGeneration, fence, forwardChild,
                  rollbackChild, forwardFailed, terminalTarget,
                  terminalVerified, freshRuntimeVerified, writerPhase,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

FreshRuntimeRead(p) ==
    /\ sessionStatus = "releasing"
    /\ activeAttempt = p
    /\ freshRuntimeVerified' = TRUE
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  controlRevision, forwardChild, rollbackChild, forwardFailed,
                  terminalTarget, terminalVerified, writerPhase,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

CompleteReopen(p) ==
    /\ sessionStatus = "releasing"
    /\ activeAttempt = p
    /\ terminalVerified
    /\ freshRuntimeVerified
    /\ controlRevision < MaxRevision
    /\ sessionStatus' = "released"
    /\ controlRevision' = controlRevision + 1
    /\ UNCHANGED <<activeAttempt, attemptGeneration, fence, forwardChild,
                  rollbackChild, forwardFailed, terminalTarget,
                  terminalVerified, freshRuntimeVerified, writerPhase,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

MarkAmbiguous(p) ==
    /\ activeAttempt = p
    /\ sessionStatus \in {"held", "releasing"}
    /\ controlRevision < MaxRevision
    /\ sessionStatus' = "ambiguous"
    /\ controlRevision' = controlRevision + 1
    /\ UNCHANGED <<activeAttempt, attemptGeneration, fence, forwardChild,
                  rollbackChild, forwardFailed, terminalTarget,
                  terminalVerified, freshRuntimeVerified, writerPhase,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

RequestWrite(p) ==
    /\ writerPhase[p] = "idle"
    /\ writerPhase' = [writerPhase EXCEPT ![p] = "requested"]
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  controlRevision, forwardChild, rollbackChild, forwardFailed,
                  terminalTarget, terminalVerified, freshRuntimeVerified,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

AcceptWrite(p) ==
    /\ writerPhase[p] = "requested"
    /\ sessionStatus \in {"absent", "released"}
    /\ writerPhase' = [writerPhase EXCEPT ![p] = "accepted"]
    /\ writerAcceptedStatus' = [writerAcceptedStatus EXCEPT ![p] = sessionStatus]
    /\ writerMutations' = writerMutations + 1
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  controlRevision, forwardChild, rollbackChild, forwardFailed,
                  terminalTarget, terminalVerified, freshRuntimeVerified,
                  writerTarget, lockOwner>>

RejectWrite(p) ==
    /\ writerPhase[p] = "requested"
    /\ sessionStatus \in UnsafeStatuses
    /\ writerPhase' = [writerPhase EXCEPT ![p] = "rejected"]
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  controlRevision, forwardChild, rollbackChild, forwardFailed,
                  terminalTarget, terminalVerified, freshRuntimeVerified,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

FinishWrite(p) ==
    /\ writerPhase[p] \in {"accepted", "rejected"}
    /\ writerPhase' = [writerPhase EXCEPT ![p] = "done"]
    /\ UNCHANGED <<sessionStatus, activeAttempt, attemptGeneration, fence,
                  controlRevision, forwardChild, rollbackChild, forwardFailed,
                  terminalTarget, terminalVerified, freshRuntimeVerified,
                  writerAcceptedStatus, writerMutations, writerTarget,
                  lockOwner>>

Next ==
    \/ (\E p \in Processes:
          Acquire(p) \/ BindForward(p) \/ ForwardFailure(p) \/ BindRollback(p)
          \/ VerifyTerminal(p, "new") \/ VerifyTerminal(p, "rollback")
          \/ BeginReopen(p) \/ FreshRuntimeRead(p) \/ CompleteReopen(p)
          \/ MarkAmbiguous(p) \/ RequestWrite(p) \/ AcceptWrite(p)
          \/ RejectWrite(p) \/ FinishWrite(p))
    \/ UNCHANGED vars

TypeOK ==
    /\ sessionStatus \in Statuses
    /\ activeAttempt \in Processes \cup {NoProcess}
    /\ attemptGeneration \in 0..MaxRevision
    /\ fence \in 0..MaxRevision
    /\ controlRevision \in 0..MaxRevision
    /\ forwardChild \in {NoChild} \cup {ForwardId(p): p \in Processes}
    /\ rollbackChild \in {NoChild} \cup {RollbackId(p): p \in Processes}
    /\ forwardFailed \in BOOLEAN
    /\ terminalTarget \in TerminalResults
    /\ terminalVerified \in BOOLEAN
    /\ freshRuntimeVerified \in BOOLEAN
    /\ writerPhase \in [Processes -> WriterPhases]
    /\ writerAcceptedStatus \in [Processes -> (Statuses \cup {"none"})]
    /\ writerMutations \in 0..MaxRevision
    /\ writerTarget \in [Processes -> {"authority"}]
    /\ lockOwner \in Processes \cup {NoProcess}

OneActiveSession ==
    sessionStatus = "absent" \/ sessionStatus \in Statuses

IdentityStable ==
    sessionStatus # "absent" =>
        /\ activeAttempt # NoProcess
        /\ fence > 0
        /\ attemptGeneration > 0

ChildIdentityStable ==
    /\ forwardChild # NoChild => forwardChild \in {ForwardId(p): p \in Processes}
    /\ rollbackChild # NoChild => rollbackChild \in {RollbackId(p): p \in Processes}
    /\ (forwardChild # NoChild /\ rollbackChild # NoChild) =>
        forwardChild # rollbackChild

NoUnheldRollbackGap ==
    rollbackChild # NoChild => sessionStatus \in {"held", "releasing", "released", "ambiguous"}

ReleaseEvidence ==
    sessionStatus = "released" => terminalVerified /\ freshRuntimeVerified

WriteFence ==
    \A p \in Processes:
        writerPhase[p] = "accepted" => writerAcceptedStatus[p] \in {"absent", "released"}

AmbiguousIsWriteClosed ==
    sessionStatus = "ambiguous" =>
        \A p \in Processes: writerPhase[p] # "accepted" \/ writerAcceptedStatus[p] # "ambiguous"

CasBounded ==
    controlRevision <= MaxRevision /\ fence <= MaxRevision

Spec == Init /\ [][Next]_vars

THEOREM Spec => []TypeOK
THEOREM Spec => []OneActiveSession
THEOREM Spec => []IdentityStable
THEOREM Spec => []ChildIdentityStable
THEOREM Spec => []NoUnheldRollbackGap
THEOREM Spec => []ReleaseEvidence
THEOREM Spec => []WriteFence
THEOREM Spec => []AmbiguousIsWriteClosed
THEOREM Spec => []CasBounded

=============================================================================

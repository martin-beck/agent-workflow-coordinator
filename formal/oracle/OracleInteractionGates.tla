----------------------------- MODULE OracleInteractionGates -----------------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS Naturals, Sequences

Stages == <<"intake", "discussion", "formal_spec_review", "reconciliation">>
Operations == {"open", "resolve", "claim", "run", "release"}
Dispositions == {"accepted", "unresolved"}
Processes == {p1, p2}
VARIABLES revision, completed, openStage, operation, requestedStage,
          expectedRevision, disposition, result
vars == <<revision, completed, openStage, operation, requestedStage,
          expectedRevision, disposition, result>>
Init == revision = 1 /\ completed = <<>> /\ openStage = "none"
    /\ operation = [p \in Processes |-> "open"]
    /\ requestedStage = [p \in Processes |-> "intake"]
    /\ expectedRevision = [p \in Processes |-> 1]
    /\ disposition = [p \in Processes |-> "unresolved"]
    /\ result = [p \in Processes |-> "waiting"]
NextStage == IF Len(completed) < Len(Stages) THEN Stages[Len(completed) + 1] ELSE "none"
Hostile(p) == expectedRevision[p] = revision /\ requestedStage[p] # NextStage
    /\ result' = [result EXCEPT ![p] = "rejected"]
    /\ UNCHANGED <<revision, completed, openStage, operation, requestedStage,
                    expectedRevision, disposition>>
Stale(p) == expectedRevision[p] # revision
    /\ result' = [result EXCEPT ![p] = "rejected"]
    /\ UNCHANGED <<revision, completed, openStage, operation, requestedStage,
                    expectedRevision, disposition>>
Open(p) == operation[p] = "open" /\ expectedRevision[p] = revision
    /\ requestedStage[p] = NextStage /\ openStage = "none"
    /\ openStage' = requestedStage[p] /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"]
    /\ UNCHANGED <<completed, operation, requestedStage, expectedRevision, disposition>>
Resolve(p) == operation[p] = "resolve" /\ expectedRevision[p] = revision
    /\ openStage # "none" /\ requestedStage[p] = openStage /\ revision' = revision + 1
    /\ IF disposition[p] = "unresolved"
          THEN UNCHANGED <<completed, openStage>>
          ELSE completed' = Append(completed, openStage) /\ openStage' = "none"
    /\ result' = [result EXCEPT ![p] = "accepted"]
    /\ UNCHANGED <<operation, requestedStage, expectedRevision, disposition>>
Blocked(p) == operation[p] \in {"claim", "run", "release"} /\ openStage # "none"
    /\ result' = [result EXCEPT ![p] = "rejected"]
    /\ UNCHANGED <<revision, completed, openStage, operation, requestedStage,
                    expectedRevision, disposition>>
Step(p) == Hostile(p) \/ Stale(p) \/ Open(p) \/ Resolve(p) \/ Blocked(p)
Next == \E p \in Processes: Step(p)
Spec == Init /\ [][Next]_vars
TypeOK == revision \in Nat /\ completed \in Seq(Stages)
    /\ openStage \in Stages \cup {"none"}
    /\ operation \in [Processes -> Operations]
    /\ requestedStage \in [Processes -> Stages]
    /\ expectedRevision \in [Processes -> Nat]
    /\ disposition \in [Processes -> Dispositions]
NoSkippedGate == openStage # "none" => openStage = NextStage
RevisionMonotonic == revision >= 1
OpenGateBlocksAutonomousWork == openStage # "none" =>
    \A p \in Processes: ~(operation[p] \in {"claim", "run", "release"}
        /\ result[p] = "accepted")
=============================================================================

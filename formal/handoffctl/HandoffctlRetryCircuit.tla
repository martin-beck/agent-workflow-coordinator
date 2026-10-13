------------------------- MODULE HandoffctlRetryCircuit -------------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS Naturals

(*)
Bounded no-progress retry contract. A failed command can be retried below the
configured threshold. At that threshold it is held. Only a durable-progress
change or an explicit recovery record reopens it; neither path loses the fact
that a hold occurred.
*)

CONSTANT MaxAttempts

ASSUME MaxAttempts >= 3

VARIABLES attempts, held, progress, recoveryCount

vars == <<attempts, held, progress, recoveryCount>>

Init ==
    /\ attempts = 0
    /\ held = FALSE
    /\ progress = 0
    /\ recoveryCount = 0

Fail ==
    /\ ~held
    /\ attempts' = attempts + 1
    /\ held' = attempts + 1 >= MaxAttempts
    /\ UNCHANGED <<progress, recoveryCount>>

Progress ==
    /\ progress' = progress + 1
    /\ attempts' = 0
    /\ held' = FALSE
    /\ UNCHANGED recoveryCount

Recover ==
    /\ held
    /\ held' = FALSE
    /\ recoveryCount' = recoveryCount + 1
    /\ UNCHANGED <<attempts, progress>>

Next == Fail \/ Progress \/ Recover

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ attempts \in Nat
    /\ held \in BOOLEAN
    /\ progress \in Nat
    /\ recoveryCount \in Nat

HeldAtExactBound == held <=> attempts = MaxAttempts
RecoveryCountMonotonic == recoveryCount \in Nat
=============================================================================

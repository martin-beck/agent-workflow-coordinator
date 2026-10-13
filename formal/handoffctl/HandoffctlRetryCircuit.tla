------------------------- MODULE HandoffctlRetryCircuit -------------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS Naturals, FiniteSets, TLC

(*
Bounded no-progress retry admission for unbound commands. A worker receives a
durable slot before launch. It either resolves that slot, crashes after launch,
or its abandoned slot expires. Expiry prevents an interrupted worker or result
recording fault from exhausting capacity forever. Bound execution envelopes are
absent: their execution fence owns reservation/result/finish sequencing.
*)

CONSTANTS MaxAttempts, Workers

ASSUME /\ MaxAttempts >= 3
       /\ Cardinality(Workers) >= MaxAttempts

SlotValues == {"idle", "reserved", "abandoned"}

VARIABLES attempts, held, recovered, slots, cleaned

vars == <<attempts, held, recovered, slots, cleaned>>

ReservedCount == Cardinality({worker \in Workers: slots[worker] = "reserved"})

Init ==
    /\ attempts = 0
    /\ held = FALSE
    /\ recovered = FALSE
    /\ slots = [worker \in Workers |-> "idle"]
    /\ cleaned = FALSE

Reserve(worker) ==
    /\ worker \in Workers
    /\ slots[worker] = "idle"
    /\ ~held
    /\ attempts + ReservedCount < MaxAttempts
    /\ slots' = [slots EXCEPT ![worker] = "reserved"]
    /\ UNCHANGED <<attempts, held, recovered, cleaned>>

ResolveFailure(worker) ==
    /\ worker \in Workers
    /\ slots[worker] = "reserved"
    /\ slots' = [slots EXCEPT ![worker] = "idle"]
    /\ attempts' = attempts + 1
    /\ held' = (attempts + 1 = MaxAttempts)
    /\ UNCHANGED <<recovered, cleaned>>

ResolveSuccess(worker) ==
    /\ worker \in Workers
    /\ slots[worker] = "reserved"
    /\ slots' = [slots EXCEPT ![worker] = "idle"]
    /\ UNCHANGED <<attempts, held, recovered, cleaned>>

Crash(worker) ==
    /\ worker \in Workers
    /\ slots[worker] = "reserved"
    /\ slots' = [slots EXCEPT ![worker] = "abandoned"]
    /\ UNCHANGED <<attempts, held, recovered, cleaned>>

Expire(worker) ==
    /\ worker \in Workers
    /\ slots[worker] = "abandoned"
    /\ slots' = [slots EXCEPT ![worker] = "idle"]
    /\ cleaned' = TRUE
    /\ UNCHANGED <<attempts, held, recovered>>

Recover ==
    /\ held
    /\ ~recovered
    /\ attempts' = 0
    /\ held' = FALSE
    /\ recovered' = TRUE
    /\ UNCHANGED <<slots, cleaned>>

Next ==
    \/ \E worker \in Workers: Reserve(worker)
    \/ \E worker \in Workers: ResolveFailure(worker)
    \/ \E worker \in Workers: ResolveSuccess(worker)
    \/ \E worker \in Workers: Crash(worker)
    \/ \E worker \in Workers: Expire(worker)
    \/ Recover
    \/ UNCHANGED vars

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ \A worker \in Workers: WF_vars(Expire(worker))

TypeOK ==
    /\ attempts \in 0..MaxAttempts
    /\ held \in BOOLEAN
    /\ recovered \in BOOLEAN
    /\ slots \in [Workers -> SlotValues]
    /\ cleaned \in BOOLEAN

CapacityBound == attempts + ReservedCount <= MaxAttempts
HeldAtExactBound == held <=> attempts = MaxAttempts
HeldHasNoLiveSlot == held => ReservedCount = 0
AbandonedSlotsEventuallyClean ==
    \A worker \in Workers: [] (slots[worker] = "abandoned" => <> (slots[worker] = "idle"))
=============================================================================

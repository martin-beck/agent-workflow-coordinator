----------------------------- MODULE DurableSessionChain -----------------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS FiniteSets, Naturals, Sequences, TLC

CONSTANTS Session, MaxRecords
VARIABLES records, ambiguous

vars == <<records, ambiguous>>
Init == records = << >> /\ ambiguous = FALSE

Digest == {"genesis", "d1", "d2", "d3", "d4"}
RecordSet == [session : {Session}, sequence : 1..MaxRecords,
  prior : Digest \cup {NULL}, digest : Digest, fsync : {"durable"}]

Append(r) ==
  /\ ~ambiguous
  /\ Len(records) < MaxRecords
  /\ r \in RecordSet
  /\ r.session = Session
  /\ r.sequence = Len(records) + 1
  /\ (Len(records) = 0 => r.prior = NULL)
  /\ (Len(records) > 0 => r.prior = records[Len(records)].digest)
  /\ r.fsync = "durable"
  /\ records' = Append(records, r)
  /\ ambiguous' = FALSE

Replay(r) ==
  /\ Len(records) > 0
  /\ r.sequence = records[Len(records)].sequence
  /\ r = records[Len(records)]
  /\ UNCHANGED vars

Reject == /\ ambiguous' = TRUE /\ UNCHANGED records

CrashAfterAppendBeforeFsync(r) ==
  /\ ~ambiguous
  /\ r.sequence = Len(records) + 1
  /\ records' = Append(records, r)
  /\ ambiguous' = TRUE

Next == \E r : Append(r) \/ Replay(r) \/ CrashAfterAppendBeforeFsync(r) \/ Reject

TypeOK == records \in Seq(RecordSet) /\ ambiguous \in BOOLEAN
Contiguous == \A i \in 1..Len(records) : records[i].sequence = i
IdentityStable == \A i \in 1..Len(records) : records[i].session = Session
AmbiguousMonotonic == ambiguous => ~ENABLED Append(records[Len(records) + 1])

Spec == Init /\ [][Next]_vars
THEOREM Spec => []TypeOK
THEOREM Spec => []Contiguous
THEOREM Spec => []IdentityStable
THEOREM Spec => []AmbiguousMonotonic

=============================================================================

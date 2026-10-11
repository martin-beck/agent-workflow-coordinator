------------------------ MODULE HandoffctlPublication ------------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS FiniteSets, TLC

(***********************************************************************)
(* Durable, candidate-bound protected-main publication contract.       *)
(* Candidate and expected base are immutable once the job is admitted. *)
(***********************************************************************)

CONSTANTS Keys, Digests, Candidate, Base, Descendant, Foreign, NoDigest, NoOwner, NoHead
ASSUME /\ Keys # {} /\ Digests # {}
       /\ Candidate # Base /\ Candidate # Descendant /\ Candidate # Foreign
       /\ Base # Descendant /\ Base # Foreign /\ Descendant # Foreign
       /\ NoDigest \notin Digests
       /\ NoOwner \notin Keys
       /\ NoHead \notin {Base, Candidate, Foreign}

Phases == {"empty", "queued", "publishing", "awaiting_observation",
           "ambiguous", "published", "rejected"}
Terminal == {"published", "rejected"}

VARIABLES requests, outcome, jobDigest, expectedBase, phase, publisher,
          remote, lastObserved, recoveryObserved, ambiguityAvailable
vars == <<requests, outcome, jobDigest, expectedBase, phase, publisher,
          remote, lastObserved, recoveryObserved, ambiguityAvailable>>

Init ==
    /\ requests = [k \in Keys |-> NoDigest]
    /\ outcome = [k \in Keys |-> "none"]
    /\ jobDigest = NoDigest
    /\ expectedBase = NoHead
    /\ phase = "empty"
    /\ publisher = NoOwner
    /\ remote = Base
    /\ lastObserved = NoHead
    /\ recoveryObserved = FALSE
    /\ ambiguityAvailable = TRUE

Admit(k, d) ==
    /\ k \in Keys /\ d \in Digests
    /\ outcome[k] = "none"
    /\ requests' = [requests EXCEPT ![k] = d]
    /\ IF jobDigest = NoDigest
          THEN /\ outcome' = [outcome EXCEPT ![k] = "accepted"]
               /\ jobDigest' = d
               /\ expectedBase' = Base
               /\ phase' = "queued"
               /\ UNCHANGED <<publisher, remote, lastObserved, recoveryObserved,
                               ambiguityAvailable>>
          ELSE IF jobDigest = d
               THEN /\ outcome' = [outcome EXCEPT ![k] = "reused"]
                    /\ UNCHANGED <<jobDigest, expectedBase, phase, publisher,
                                     remote, lastObserved, recoveryObserved,
                                     ambiguityAvailable>>
               ELSE /\ outcome' = [outcome EXCEPT ![k] = "rejected"]
                    /\ UNCHANGED <<jobDigest, expectedBase, phase, publisher,
                                     remote, lastObserved, recoveryObserved,
                                     ambiguityAvailable>>

Claim(k) ==
    /\ k \in Keys
    /\ phase = "queued" /\ publisher = NoOwner
    /\ publisher' = k
    /\ phase' = "publishing"
    /\ recoveryObserved' = FALSE
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, remote, lastObserved,
                    ambiguityAvailable>>

PublishCAS ==
    /\ phase = "publishing" /\ publisher \in Keys
    /\ remote = expectedBase
    /\ remote' = Candidate
    /\ phase' = "awaiting_observation"
    /\ publisher' = NoOwner
    /\ lastObserved' = NoHead
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, recoveryObserved,
                    ambiguityAvailable>>

RejectStaleBase ==
    /\ phase = "publishing" /\ publisher \in Keys
    /\ remote # expectedBase
    /\ phase' = "rejected"
    /\ publisher' = NoOwner
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, remote,
                    lastObserved, recoveryObserved, ambiguityAvailable>>

ObservePush ==
    /\ phase = "awaiting_observation"
    /\ remote = Candidate
    /\ lastObserved' = remote
    /\ phase' = "published"
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, publisher,
                    remote, recoveryObserved, ambiguityAvailable>>

ObserveRemoteAdvanced ==
    /\ phase = "awaiting_observation"
    /\ remote = Descendant
    /\ lastObserved' = remote
    /\ phase' = "rejected"
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, publisher,
                    remote, recoveryObserved, ambiguityAvailable>>

PushAmbiguousBeforeEffect ==
    /\ phase = "publishing" /\ publisher \in Keys
    /\ ambiguityAvailable
    /\ phase' = "ambiguous"
    /\ publisher' = NoOwner
    /\ lastObserved' = NoHead
    /\ recoveryObserved' = FALSE
    /\ ambiguityAvailable' = FALSE
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, remote>>

PushAmbiguousAfterEffect ==
    /\ phase = "publishing" /\ publisher \in Keys
    /\ ambiguityAvailable
    /\ remote = expectedBase
    /\ remote' = Candidate
    /\ phase' = "ambiguous"
    /\ publisher' = NoOwner
    /\ lastObserved' = NoHead
    /\ recoveryObserved' = FALSE
    /\ ambiguityAvailable' = FALSE
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase>>

ExpirePublisherLease ==
    /\ phase = "publishing" /\ publisher \in Keys
    /\ phase' = "ambiguous"
    /\ publisher' = NoOwner
    /\ lastObserved' = NoHead
    /\ recoveryObserved' = FALSE
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, remote,
                    ambiguityAvailable>>

ObserveRecovery ==
    /\ phase = "ambiguous" /\ publisher = NoOwner
    /\ lastObserved' = remote
    /\ recoveryObserved' = TRUE
    /\ IF remote = Candidate
          THEN phase' = "published"
          ELSE IF remote = expectedBase
               THEN phase' = "queued"
               ELSE phase' = "rejected"
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, publisher, remote,
                    ambiguityAvailable>>

AdvanceRemote ==
    /\ jobDigest # NoDigest
    /\ remote = Base
    /\ remote' = Foreign
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, phase,
                    publisher, lastObserved, recoveryObserved, ambiguityAvailable>>

AdvanceRemoteAfterCandidate ==
    /\ jobDigest # NoDigest
    /\ phase = "awaiting_observation"
    /\ remote = Candidate
    /\ remote' = Descendant
    /\ UNCHANGED <<requests, outcome, jobDigest, expectedBase, phase,
                    publisher, lastObserved, recoveryObserved, ambiguityAvailable>>

TerminalStutter ==
    /\ phase \in Terminal
    /\ UNCHANGED vars

Next ==
    (\E k \in Keys, d \in Digests: Admit(k, d))
    \/ (\E k \in Keys: Claim(k))
    \/ PublishCAS \/ RejectStaleBase \/ ObservePush \/ ObserveRemoteAdvanced
    \/ PushAmbiguousBeforeEffect \/ PushAmbiguousAfterEffect
    \/ ExpirePublisherLease \/ ObserveRecovery \/ AdvanceRemote
    \/ AdvanceRemoteAfterCandidate \/ TerminalStutter

Fairness ==
    /\ \A k \in Keys: WF_vars(Claim(k))
    /\ WF_vars(PublishCAS)
    /\ WF_vars(RejectStaleBase)
    /\ WF_vars(ObservePush)
    /\ WF_vars(ObserveRemoteAdvanced)
    /\ WF_vars(ExpirePublisherLease)
    /\ WF_vars(ObserveRecovery)

Spec == Init /\ [][Next]_vars /\ Fairness

TypeOK ==
    /\ requests \in [Keys -> {NoDigest} \union Digests]
    /\ outcome \in [Keys -> {"none", "accepted", "reused", "rejected"}]
    /\ jobDigest \in {NoDigest} \union Digests
    /\ expectedBase \in {NoHead, Base}
    /\ phase \in Phases
    /\ publisher \in {NoOwner} \union Keys
    /\ remote \in {Base, Candidate, Descendant, Foreign}
    /\ lastObserved \in {NoHead, Base, Candidate, Descendant, Foreign}
    /\ recoveryObserved \in BOOLEAN
    /\ ambiguityAvailable \in BOOLEAN

OneDurableJob ==
    phase = "empty" <=> jobDigest = NoDigest

CandidateAndBaseStayBound ==
    jobDigest # NoDigest => expectedBase = Base

OnePublisherLease ==
    (publisher \in Keys) <=> phase = "publishing"

ReuseIsIdempotent ==
    \A k \in Keys: outcome[k] = "reused" => requests[k] = jobDigest

ChangedDescriptorRejected ==
    \A k \in Keys: outcome[k] = "rejected" => requests[k] # jobDigest

PublishedOnlyAfterObservation ==
    phase = "published" => lastObserved = Candidate /\ remote = Candidate

RecoveryRetryRequiresFreshBaseObservation ==
    phase = "queued" /\ recoveryObserved => lastObserved = expectedBase

NoUnobservedAmbiguousSuccess ==
    phase = "ambiguous" => ~recoveryObserved

AdmittedJobTerminates ==
    (jobDigest # NoDigest) ~> (phase \in Terminal)

=============================================================================

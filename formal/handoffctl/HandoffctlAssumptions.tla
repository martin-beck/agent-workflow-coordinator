---------------------- MODULE HandoffctlAssumptions ----------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS Naturals, TLC

(*******************************************************************************
Bounded contract for opt-in freshness-sensitive task assumptions.  A declared
base head and dependency revision may move independently.  Material stale
bindings require an explicit revalidation or disposition before done; an
explicitly non-material binding remains explainable without universally
blocking progress.
*******************************************************************************)

CONSTANTS Processes, Actors, NoActor

Heads == {"head-a", "head-b"}
DependencyRevisions == {1, 2}
Actions == {"declare-base", "declare-dependency", "revalidate-base",
            "revalidate-dependency", "dispose-base", "dispose-dependency", "release-done"}
Phases == {"ready", "done"}
Results == {"pending", "accepted", "rejected"}

VARIABLES status, owner, revision, currentBase, currentDependency,
          baseDeclared, dependencyDeclared, baseMaterial, dependencyMaterial,
          boundBase, boundDependency, baseDisposed, dependencyDisposed,
          pc, action, requestedActor, expectedRevision, result

vars == <<status, owner, revision, currentBase, currentDependency, baseDeclared,
          dependencyDeclared, baseMaterial, dependencyMaterial, boundBase,
          boundDependency, baseDisposed, dependencyDisposed, pc, action,
          requestedActor, expectedRevision, result>>

BaseFresh == ~baseDeclared \/ ~baseMaterial \/ baseDisposed \/ boundBase = currentBase
DependencyFresh == ~dependencyDeclared \/ ~dependencyMaterial \/ dependencyDisposed \/ boundDependency = currentDependency
Fresh == BaseFresh /\ DependencyFresh

Init ==
    /\ status = "in_progress"
    /\ owner = "owner-a"
    /\ revision = 0
    /\ currentBase = "head-a"
    /\ currentDependency = 1
    /\ baseDeclared = FALSE /\ dependencyDeclared = FALSE
    /\ baseMaterial \in BOOLEAN /\ dependencyMaterial \in BOOLEAN
    /\ boundBase = "head-a" /\ boundDependency = 1
    /\ baseDisposed = FALSE /\ dependencyDisposed = FALSE
    /\ pc = [p \in Processes |-> "ready"]
    /\ action \in [Processes -> Actions]
    /\ requestedActor \in [Processes -> Actors]
    /\ expectedRevision \in [Processes -> 0..2]
    /\ result = [p \in Processes |-> "pending"]

DeclareBaseEnabled(p) ==
    /\ pc[p] = "ready" /\ action[p] = "declare-base" /\ ~baseDeclared
    /\ requestedActor[p] = owner /\ expectedRevision[p] = revision

DeclareBase(p) ==
    /\ DeclareBaseEnabled(p)
    /\ baseDeclared' = TRUE /\ boundBase' = currentBase /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<status, owner, currentBase, currentDependency, dependencyDeclared,
                  baseMaterial, dependencyMaterial, boundDependency, baseDisposed,
                  dependencyDisposed, action, requestedActor, expectedRevision>>

DeclareDependencyEnabled(p) ==
    /\ pc[p] = "ready" /\ action[p] = "declare-dependency" /\ ~dependencyDeclared
    /\ requestedActor[p] = owner /\ expectedRevision[p] = revision

DeclareDependency(p) ==
    /\ DeclareDependencyEnabled(p)
    /\ dependencyDeclared' = TRUE /\ boundDependency' = currentDependency /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<status, owner, currentBase, currentDependency, baseDeclared,
                  baseMaterial, dependencyMaterial, boundBase, baseDisposed,
                  dependencyDisposed, action, requestedActor, expectedRevision>>

RevalidateBaseEnabled(p) ==
    /\ pc[p] = "ready" /\ action[p] = "revalidate-base" /\ baseDeclared /\ ~baseDisposed
    /\ requestedActor[p] = owner /\ expectedRevision[p] = revision /\ boundBase # currentBase

RevalidateBase(p) ==
    /\ RevalidateBaseEnabled(p)
    /\ boundBase' = currentBase /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<status, owner, currentBase, currentDependency, baseDeclared,
                  dependencyDeclared, baseMaterial, dependencyMaterial, boundDependency,
                  baseDisposed, dependencyDisposed, action, requestedActor, expectedRevision>>

RevalidateDependencyEnabled(p) ==
    /\ pc[p] = "ready" /\ action[p] = "revalidate-dependency" /\ dependencyDeclared /\ ~dependencyDisposed
    /\ requestedActor[p] = owner /\ expectedRevision[p] = revision /\ boundDependency # currentDependency

RevalidateDependency(p) ==
    /\ RevalidateDependencyEnabled(p)
    /\ boundDependency' = currentDependency /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<status, owner, currentBase, currentDependency, baseDeclared,
                  dependencyDeclared, baseMaterial, dependencyMaterial, boundBase,
                  baseDisposed, dependencyDisposed, action, requestedActor, expectedRevision>>

DisposeBaseEnabled(p) ==
    /\ pc[p] = "ready" /\ action[p] = "dispose-base" /\ baseDeclared /\ ~baseDisposed
    /\ requestedActor[p] = owner /\ expectedRevision[p] = revision

DisposeBase(p) ==
    /\ DisposeBaseEnabled(p)
    /\ baseDisposed' = TRUE /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<status, owner, currentBase, currentDependency, baseDeclared,
                  dependencyDeclared, baseMaterial, dependencyMaterial, boundBase,
                  boundDependency, dependencyDisposed, action, requestedActor, expectedRevision>>

DisposeDependencyEnabled(p) ==
    /\ pc[p] = "ready" /\ action[p] = "dispose-dependency" /\ dependencyDeclared /\ ~dependencyDisposed
    /\ requestedActor[p] = owner /\ expectedRevision[p] = revision

DisposeDependency(p) ==
    /\ DisposeDependencyEnabled(p)
    /\ dependencyDisposed' = TRUE /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<status, owner, currentBase, currentDependency, baseDeclared,
                  dependencyDeclared, baseMaterial, dependencyMaterial, boundBase,
                  boundDependency, baseDisposed, action, requestedActor, expectedRevision>>

ReleaseDoneEnabled(p) ==
    /\ pc[p] = "ready" /\ action[p] = "release-done" /\ requestedActor[p] = owner /\ Fresh

ReleaseDone(p) ==
    /\ ReleaseDoneEnabled(p)
    /\ status' = "done" /\ owner' = NoActor /\ revision' = revision + 1
    /\ result' = [result EXCEPT ![p] = "accepted"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<currentBase, currentDependency, baseDeclared, dependencyDeclared,
                  baseMaterial, dependencyMaterial, boundBase, boundDependency,
                  baseDisposed, dependencyDisposed, action, requestedActor, expectedRevision>>

Reject(p) ==
    /\ pc[p] = "ready"
    /\ ~DeclareBaseEnabled(p) /\ ~DeclareDependencyEnabled(p) /\ ~RevalidateBaseEnabled(p)
    /\ ~RevalidateDependencyEnabled(p) /\ ~DisposeBaseEnabled(p)
    /\ ~DisposeDependencyEnabled(p) /\ ~ReleaseDoneEnabled(p)
    /\ result' = [result EXCEPT ![p] = "rejected"] /\ pc' = [pc EXCEPT ![p] = "done"]
    /\ UNCHANGED <<status, owner, revision, currentBase, currentDependency, baseDeclared,
                  dependencyDeclared, baseMaterial, dependencyMaterial, boundBase,
                  boundDependency, baseDisposed, dependencyDisposed, action,
                  requestedActor, expectedRevision>>

MoveBase == /\ status = "in_progress" /\ currentBase' \in Heads /\ currentBase' # currentBase
            /\ UNCHANGED <<status, owner, revision, currentDependency, baseDeclared,
                          dependencyDeclared, baseMaterial, dependencyMaterial, boundBase,
                          boundDependency, baseDisposed, dependencyDisposed, pc, action,
                          requestedActor, expectedRevision, result>>
MoveDependency == /\ status = "in_progress" /\ currentDependency' \in DependencyRevisions /\ currentDependency' # currentDependency
                  /\ UNCHANGED <<status, owner, revision, currentBase, baseDeclared,
                                dependencyDeclared, baseMaterial, dependencyMaterial, boundBase,
                                boundDependency, baseDisposed, dependencyDisposed, pc, action,
                                requestedActor, expectedRevision, result>>

Quiescent == /\ \A p \in Processes: pc[p] = "done" /\ UNCHANGED vars

Next == (\E p \in Processes: DeclareBase(p) \/ DeclareDependency(p) \/ RevalidateBase(p) \/ RevalidateDependency(p) \/ DisposeBase(p) \/ DisposeDependency(p) \/ ReleaseDone(p) \/ Reject(p)) \/ MoveBase \/ MoveDependency \/ Quiescent
Spec == Init /\ [][Next]_vars

TypeOK == /\ status \in {"in_progress", "done"} /\ owner \in Actors \cup {NoActor}
          /\ revision \in Nat /\ currentBase \in Heads /\ currentDependency \in DependencyRevisions
          /\ baseDeclared \in BOOLEAN /\ dependencyDeclared \in BOOLEAN
          /\ baseMaterial \in BOOLEAN /\ dependencyMaterial \in BOOLEAN
          /\ boundBase \in Heads /\ boundDependency \in DependencyRevisions
          /\ baseDisposed \in BOOLEAN /\ dependencyDisposed \in BOOLEAN
          /\ pc \in [Processes -> Phases] /\ result \in [Processes -> Results]
          /\ action \in [Processes -> Actions] /\ requestedActor \in [Processes -> Actors]
          /\ expectedRevision \in [Processes -> Nat]
DoneRequiresFreshMaterialAssumptions == status = "done" => Fresh
DisposedAssumptionsAreExplicit ==
    /\ (baseDisposed => baseDeclared)
    /\ (dependencyDisposed => dependencyDeclared)
NonmaterialDoesNotUniversallyBlock ==
    /\ (~baseMaterial => BaseFresh)
    /\ (~dependencyMaterial => DependencyFresh)
=============================================================================

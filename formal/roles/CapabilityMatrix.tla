-------------------- MODULE CapabilityMatrix --------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT

EXTENDS FiniteSets, Naturals, TLC

(***************************************************************************)
(* Bounded capability semantics. Assignments are descriptive authorization  *)
(* input; an executed mutation must retain the role evidence that admitted  *)
(* it, reviewers are distinct from executors, and security work retains the *)
(* security role.                                                        *)
(***************************************************************************)

CONSTANTS Owners, Roles, Tasks, Actions, NoOwner, NoReviewer,
          MutationActions, SecurityTasks, ImplementerRole, ReviewerRole,
          SecurityRole

ASSUME /\ Owners # {} /\ Roles # {} /\ Tasks # {} /\ Actions # {}
    /\ NoOwner \notin Owners /\ NoReviewer \notin Owners
    /\ ImplementerRole \in Roles /\ ReviewerRole \in Roles
    /\ SecurityRole \in Roles /\ MutationActions \subseteq Actions
    /\ SecurityTasks \subseteq Tasks

TaskStatuses == {"pending", "executed"}

VARIABLES assignments, executor, reviewer, taskAction, taskStatus

vars == <<assignments, executor, reviewer, taskAction, taskStatus>>

TypeOK ==
    /\ assignments \in [Owners -> SUBSET Roles]
    /\ executor \in [Tasks -> (Owners \cup {NoOwner})]
    /\ reviewer \in [Tasks -> (Owners \cup {NoReviewer})]
    /\ taskAction \in [Tasks -> Actions]
    /\ taskStatus \in [Tasks -> TaskStatuses]

Init ==
    /\ assignments \in [Owners -> SUBSET Roles]
    /\ executor = [t \in Tasks |-> NoOwner]
    /\ reviewer = [t \in Tasks |-> NoReviewer]
    /\ taskAction \in [Tasks -> Actions]
    /\ taskStatus = [t \in Tasks |-> "pending"]

Execute(t, owner) ==
    /\ t \in Tasks
    /\ owner \in Owners
    /\ taskStatus[t] = "pending"
    /\ taskAction[t] \in MutationActions
    /\ ImplementerRole \in assignments[owner]
    /\ (t \in SecurityTasks => SecurityRole \in assignments[owner])
    /\ taskStatus' = [taskStatus EXCEPT ![t] = "executed"]
    /\ executor' = [executor EXCEPT ![t] = owner]
    /\ UNCHANGED <<assignments, reviewer, taskAction>>

Review(t, owner) ==
    /\ t \in Tasks
    /\ owner \in Owners
    /\ taskStatus[t] = "executed"
    /\ reviewer[t] = NoReviewer
    /\ owner # executor[t]
    /\ ReviewerRole \in assignments[owner]
    /\ reviewer' = [reviewer EXCEPT ![t] = owner]
    /\ UNCHANGED <<assignments, executor, taskAction, taskStatus>>

Skip == UNCHANGED vars

Next ==
    \E t \in Tasks, owner \in Owners:
        Execute(t, owner) \/ Review(t, owner)
    \/ Skip

NoMutationWithoutRoleAuthorization ==
    \A t \in Tasks:
        taskStatus[t] = "executed" =>
            /\ executor[t] # NoOwner
            /\ taskAction[t] \in MutationActions
            /\ ImplementerRole \in assignments[executor[t]]

ReviewerDistinctFromExecutor ==
    \A t \in Tasks:
        reviewer[t] # NoReviewer => reviewer[t] # executor[t]

SecurityTaskRequiresSecurityRole ==
    \A t \in Tasks:
        t \in SecurityTasks /\ taskStatus[t] = "executed" =>
            SecurityRole \in assignments[executor[t]]

Spec == Init /\ [][Next]_vars

====================================================================

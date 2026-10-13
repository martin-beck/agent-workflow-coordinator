----------------------- MODULE HandoffctlAutonomyLimits -----------------------
\* Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
\* SPDX-License-Identifier: MIT
EXTENDS Naturals, TLC

CONSTANTS MaxCommands, MaxElapsed

VARIABLES revision, commands, elapsed, effects, held, heldRevision

vars == <<revision, commands, elapsed, effects, held, heldRevision>>

Init == /\ revision = 1 /\ commands = 0 /\ elapsed = 0 /\ effects = 0
        /\ held = FALSE /\ heldRevision = 0

Run == /\ ~held /\ commands < MaxCommands /\ elapsed < MaxElapsed
       /\ revision' = revision + 1 /\ commands' = commands + 1 /\ effects' = effects + 1
       /\ elapsed' = elapsed
       /\ held' = (commands + 1 = MaxCommands)
       /\ heldRevision' = IF commands + 1 = MaxCommands THEN revision + 1 ELSE 0

Expire == /\ ~held /\ elapsed < MaxElapsed
          /\ revision' = revision + 1 /\ elapsed' = elapsed + 1
          /\ commands' = commands /\ effects' = effects
          /\ held' = (elapsed + 1 = MaxElapsed)
          /\ heldRevision' = IF elapsed + 1 = MaxElapsed THEN revision + 1 ELSE 0

Dispose == /\ held /\ heldRevision = revision
           /\ revision' = revision + 1 /\ held' = FALSE /\ heldRevision' = 0
           /\ UNCHANGED <<commands, elapsed, effects>>

BypassAttempt == /\ held /\ UNCHANGED vars

Next == Run \/ Expire \/ Dispose \/ BypassAttempt
Spec == Init /\ [][Next]_vars

TypeOK == /\ revision \in Nat /\ commands \in Nat /\ elapsed \in Nat /\ effects \in Nat
          /\ held \in BOOLEAN /\ heldRevision \in Nat
FencedHold == held => heldRevision = revision
NoEffectBypass == held => effects <= commands
=============================================================================

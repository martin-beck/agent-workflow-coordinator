# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Structural guards for the bounded predicate-ledger formal contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORMAL = ROOT / "formal/handoffctl"


class PredicateLedgerFormalModelTests(unittest.TestCase):
    def test_model_represents_candidate_reopen_history_and_done_admission(self) -> None:
        model = (FORMAL / "HandoffctlPredicateLedger.tla").read_text(encoding="utf-8")
        for fragment in (
            "candidateRef",
            "candidateDigest",
            "AcceptEnabled(p)",
            "CandidateChanged(p) /\\ requestedReopen[p]",
            "RequestedCandidateHasNoStaleHistory(p)",
            "MarkPassesStale(history)",
            "PredicateComplete",
            "DoneRequiresCompletePredicates",
            "StaleEvidenceCannotComplete",
            "StaleEvidenceIsHistorical",
        ):
            self.assertIn(fragment, model)

    def test_model_is_a_required_exact_head_publication_input(self) -> None:
        config = (FORMAL / "HandoffctlPredicateLedger.cfg").read_text(encoding="utf-8")
        verifier = (FORMAL / "verify.sh").read_text(encoding="utf-8")
        tiers = json.loads((ROOT / "formal/tier-evidence.json").read_text(encoding="utf-8"))
        self.assertIn('Processes = {"p1", "p2"}', config)
        self.assertIn("run_model HandoffctlPredicateLedger", verifier)
        self.assertIn(
            "HandoffctlPredicateLedger",
            tiers["profiles"]["pr-publication"]["models"],
        )
        self.assertIn(
            "HandoffctlPredicateLedger",
            tiers["profiles"]["full-exhaustive"]["models"],
        )


if __name__ == "__main__":
    unittest.main()

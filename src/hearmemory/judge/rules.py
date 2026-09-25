"""RuleJudge: deterministic rule judgments (provider="rule", rule_id required).

The extractor decides rule-decidable candidates (candidate.rule_hint + meta.rule_label); RuleJudge only turns
them into Judgment rows, so rule logic lives in one place (extract.py) and is fully testable there.
No lexical heuristics: without Jev, undecidable candidates stay pending.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C
from hearmemory.judge.templates import RULE_EXTRA_LABELS, RULE_IDS

RULE_TEMPLATE = {"A1_same_resolved_path": "A1", "A2_shared_run_id": "A2", "A2_different_commit_runs": "A2",
                 "A3_near_identical": "A3", "B1_test_status": "B1", "B1_test_status_changed": "B1"}


def rule_label_ok(rule_id: Optional[str], template_id: str, label: Optional[str]) -> bool:
    if rule_id not in RULE_IDS or RULE_TEMPLATE.get(rule_id) != template_id or not label:
        return False
    if rule_id in RULE_EXTRA_LABELS:
        return label == RULE_EXTRA_LABELS[rule_id]
    return label in I.TEMPLATE_LABELS[template_id]


class RuleJudge:
    """JudgeAPI for provider "rule"."""
    name = "rule"

    def __init__(self, clock: Optional[C.Clock] = None) -> None:
        self.clock = clock

    def judge(self, cands: Sequence[I.Candidate], deadline_s: float = 5.0) -> List[I.Judgment]:
        out: List[I.Judgment] = []
        ts = C.now_ts(self.clock)
        for c in cands:
            rid = c.rule_hint
            label = (c.meta or {}).get("rule_label")
            if not rid or not rule_label_ok(rid, c.template_id, label):
                continue
            out.append(I.Judgment(
                judgment_id=I.stable_id("j-", c.candidate_id, "rule", rid), candidate_id=c.candidate_id,
                template_id=c.template_id, template_version=c.template_version, input_hash=c.input_hash,
                provider="rule", outcome="valid", ts=ts, label=label, probabilities={label: 1.0}, confidence=1.0,
                rule_id=rid))
        return out

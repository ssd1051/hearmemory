"""Judgment cache: judgments.jsonl IS the cache. Same (model, template version, input_hash) -> reuse the
valid Jev judgment as a provider="cache" row: no network, no charge, never paid twice."""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from hearmemory import interfaces as I
from hearmemory.judge import _compat as C


class JudgmentCache:
    def __init__(self) -> None:
        self._by_key: Dict[str, I.Judgment] = {}

    @classmethod
    def from_judgments(cls, js: Iterable[I.Judgment]) -> "JudgmentCache":
        c = cls()
        for j in js:
            c.add(j)
        return c

    @classmethod
    def from_store(cls, store) -> "JudgmentCache":
        try:
            return cls.from_judgments(store.iter_judgments())
        except Exception:
            return cls()

    def add(self, j: I.Judgment) -> None:
        if j.provider != "jev" or j.outcome != "valid" or not j.label:
            return
        key = I.judge_cache_key("jev", j.model_requested or j.model_returned, j.template_version, j.input_hash)
        self._by_key.setdefault(key, j)

    def __len__(self) -> int:
        return len(self._by_key)

    def lookup(self, cand: I.Candidate, model: str, clock: Optional[C.Clock] = None) -> Optional[I.Judgment]:
        orig = self._by_key.get(I.judge_cache_key("jev", model, cand.template_version, cand.input_hash))
        if orig is None:
            return None
        return I.Judgment(
            judgment_id=I.stable_id("j-", cand.candidate_id, "cache", orig.judgment_id), candidate_id=cand.candidate_id,
            template_id=cand.template_id, template_version=cand.template_version, input_hash=cand.input_hash,
            provider="cache", outcome="valid", ts=C.now_ts(clock), label=orig.label,
            probabilities=dict(orig.probabilities), confidence=orig.confidence, model_requested=orig.model_requested,
            model_returned=orig.model_returned, cached_from=orig.judgment_id, input_tokens=0, output_tokens=0,
            est_usd=0.0)

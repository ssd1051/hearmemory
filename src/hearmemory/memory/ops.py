"""Consumers: Judgment -> MemoryOp.

Pure functions. Rules held here:
  * no valid judgment -> no operation at all (transport / validation / budget / disabled rows write nothing);
  * model judgments only produce "provisional" edges; "verified" needs a deterministic rule id
    (VERIFY_RULES); a model can never verify;
  * A3 needs BOTH directions; a single direction yields nothing;
  * B1 refutes always carries its counter-evidence ids (the refuted tag is never rendered without it);
  * "outdated" (rule B1_test_status_changed) is a program status, never a refutation.
The builder (build.py) applies the ops in (ts, id) order and enforces the actor gate
(same_actor_after_link) before calling A1/A2/A3 consumers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from hearmemory.interfaces import (TEMPLATE_LABELS, Candidate, Claim, Judgment, MemoryOp, canonical_json, issue_id_for,
                              stable_id)

VERIFY_RULES: Mapping[str, Tuple[str, ...]] = {"A1": ("A1_same_resolved_path",), "A2": ("A2_shared_run_id",)}
OUTDATED_RULES: Tuple[str, ...] = ("B1_test_status_changed",)
B1_STATUS: Mapping[str, str] = {"supports": "supported", "refutes": "refuted", "both": "disputed",
                                "insufficient": "insufficient", "outdated": "outdated"}
GUARD_FOR: Mapping[str, str] = {"same_object": "distinct_object", "same_event": "distinct_event"}
A3_SCOPE_GATE = "A3_scope_mismatch"
# one low-confidence Jev "both" (p=0.40, supports 0.03, confidence 0.21) over a patch that
# IMPLEMENTED the claim turned a true claim into [DISPUTED] + an open issue. A model's negative label
# (refutes / both) now only counts when it is decisive; otherwise it is "insufficient" -- or "supports"
# when the program itself checked a matching run (meta.direct_support_ids). A decisive "refutes" against
# program-checked support is at most "both" (disputed), never an outright [REFUTED].
B1_NEGATIVE_LABELS: Tuple[str, ...] = ("refutes", "both")
B1_NEG_MIN_PROB = 0.5
B1_MIN_SUPPORT_CONF = 0.65        # default of judge.b1_min_support_confidence
WEAK_SUPPORT_GATE = "b1_support_low_confidence"
B1_NEG_MIN_MARGIN = 0.2


@dataclass
class Decision:
    """One applicable decision about a candidate: a valid judgment or a manual override event."""
    candidate: Candidate
    label: str
    ref: str                        # judgment id / event id
    ts: str
    provider: str                   # jev | rule | cache | manual
    rule_id: Optional[str] = None
    gate: Optional[str] = None      # why the model's label was changed (counted by the builder)
    model_label: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.provider == "rule" and self.rule_id in VERIFY_RULES.get(self.candidate.template_id, ())


def decision_from_judgment(cand: Candidate, j: Judgment,
                           min_support_confidence: float = B1_MIN_SUPPORT_CONF) -> Optional[Decision]:
    """Only outcome == valid with a label in the template's label set (or the program status 'outdated' from its
    rule) becomes a decision; everything else writes nothing."""
    if j is None or j.outcome != "valid" or not j.label:
        return None
    label = j.label
    if cand.template_id == "B1" and (label == "outdated" or j.rule_id in OUTDATED_RULES):
        if j.provider != "rule":
            return None             # only the program rule may mark a claim outdated
        label = "outdated"
    elif label not in TEMPLATE_LABELS.get(cand.template_id, ()):
        return None
    gate = None
    if cand.template_id == "B1":
        label, gate = b1_effective_label(cand, j, min_support_confidence)
    return Decision(candidate=cand, label=label, ref=j.judgment_id, ts=j.ts, provider=j.provider, rule_id=j.rule_id,
                    gate=gate, model_label=j.label if gate else None)


def b1_effective_label(cand: Candidate, j: Judgment,
                       min_support_confidence: float = B1_MIN_SUPPORT_CONF) -> Tuple[str, Optional[str]]:
    """(label to apply, gate name or None). Only MODEL judgments (jev / cache) are gated; rule and manual
    decisions are authoritative. Judgments without probabilities (older rows) are taken as decisive.
    a model "supports" below `min_support_confidence` with no program-checked supporting run is
    gated "b1_support_low_confidence" (status weak_support, see consume_b1)."""
    label = j.label
    if j.provider in ("jev", "cache") and label == "supports" and j.confidence is not None \
            and float(j.confidence) < float(min_support_confidence) \
            and not (cand.meta or {}).get("direct_support_ids"):
        return label, WEAK_SUPPORT_GATE
    if j.provider not in ("jev", "cache") or label not in B1_NEGATIVE_LABELS:
        return label, None
    probs = j.probabilities or {}
    p = float(probs.get(label, 0.0) or 0.0)
    p_sup = float(probs.get("supports", 0.0) or 0.0)
    decisive = not probs or (p >= B1_NEG_MIN_PROB and p - p_sup >= B1_NEG_MIN_MARGIN)
    direct = bool((cand.meta or {}).get("direct_support_ids"))
    if not decisive:
        return ("supports" if direct else "insufficient"), "b1_negative_not_decisive"
    if direct and label == "refutes":
        return "both", "b1_refutes_vs_program_support"
    return label, None


# ---------------------------------------------------------------------------
# candidate shape helpers (tolerant: meta keys first, then subject_key parsing)
# ---------------------------------------------------------------------------
def pair_nodes(cand: Candidate) -> Tuple[Optional[str], Optional[str]]:
    m = cand.meta or {}
    a, b = m.get("node_a"), m.get("node_b")
    if a and b:
        return str(a), str(b)
    sk = cand.subject_key or ""
    if sk.startswith("pair:") and "|" in sk:
        a, b = sk[5:].split("|", 1)
        return a or None, b or None
    return None, None


def claim_pair(cand: Candidate) -> Tuple[Optional[str], Optional[str]]:
    m = cand.meta or {}
    return m.get("claim_a"), m.get("claim_b")


def b1_claim_id(cand: Candidate) -> Optional[str]:
    m = cand.meta or {}
    if m.get("claim_id"):
        return str(m["claim_id"])
    sk = cand.subject_key or ""
    if sk.startswith("claim:"):
        return sk[6:].split("|", 1)[0] or None
    return None


def b1_evidence(cand: Candidate, claim_obs_id: Optional[str]) -> List[str]:
    m = cand.meta or {}
    ev = m.get("evidence_obs_ids")
    if not ev:
        ev = [o for o in cand.basis_obs_ids or [] if o != claim_obs_id]
    return list(dict.fromkeys(str(e) for e in ev if e and e != claim_obs_id))


def obs_of_node(node: Optional[str]) -> Optional[str]:
    if not node:
        return None
    return node.split("#", 1)[0]


def scope_ok(cands: Sequence[Candidate]) -> bool:
    for c in cands:
        gates = (c.meta or {}).get("gates") or []
        if A3_SCOPE_GATE in gates or (c.meta or {}).get("scope_match") is False:
            return False
    return True


def _op(kind: str, target: Mapping[str, Any], basis: Sequence[str], ref: str, template_id: Optional[str],
        reason: str = "") -> MemoryOp:
    return MemoryOp(op_id=stable_id("op-", kind, canonical_json(dict(target)), ref), kind=kind, target=dict(target),
                    basis_obs_ids=sorted(set(basis)), decision_ref=ref, template_id=template_id, reason=reason)


# ---------------------------------------------------------------------------
# A: alignment
# ---------------------------------------------------------------------------
def consume_a1(d: Decision, issues_from_unresolved: bool = False) -> List[MemoryOp]:
    a, b = pair_nodes(d.candidate)
    if not a or not b:
        return []
    basis = list(d.candidate.basis_obs_ids or [])
    if d.label == "same":
        return [_op("edge_add", {"relation": "same_object", "a": a, "b": b, "directed": False,
                                 "status": "verified" if d.verified else "provisional"}, basis, d.ref, "A1",
                    "A1 same" + (f" (rule {d.rule_id})" if d.verified else ""))]
    if d.label == "different":
        return [_op("mark_add", {"mark": "distinct_object", "a": a, "b": b}, basis, d.ref, "A1", "A1 different")]
    out = [_op("mark_add", {"mark": "pending_alignment", "a": a, "b": b}, basis, d.ref, "A1", "A1 unresolved")]
    if issues_from_unresolved:
        title = f"Same object? {a} / {b}"
        out.append(_op("issue_open", {"issue_id": issue_id_for("manual", "align:" + "|".join(sorted((a, b)))),
                                      "kind": "manual", "title": title, "claim_id": None}, basis, d.ref, "A1",
                       "A1 unresolved -> issue"))
    return out


def consume_a2(d: Decision) -> List[MemoryOp]:
    a, b = pair_nodes(d.candidate)
    if not a or not b:
        return []
    basis = list(d.candidate.basis_obs_ids or [])
    if d.label == "same_event":
        return [_op("edge_add", {"relation": "same_event", "a": a, "b": b, "directed": False,
                                 "status": "verified" if d.verified else "provisional"}, basis, d.ref, "A2",
                    "A2 same_event" + (f" (rule {d.rule_id})" if d.verified else ""))]
    if d.label == "different_events":
        return [_op("mark_add", {"mark": "distinct_event", "a": a, "b": b}, basis, d.ref, "A2", "A2 different")]
    return [_op("mark_add", {"mark": "pending_event_alignment", "a": a, "b": b}, basis, d.ref, "A2",
                "A2 unresolved")]


def consume_a3(d_ab: Decision, d_ba: Decision, a_node: str, b_node: str, scope_match: bool,
               guarded: bool) -> List[MemoryOp]:
    """d_ab: record_a (container) vs record_b claim ("a_contains_b"); d_ba the reverse. Both required."""
    if d_ab is None or d_ba is None:
        return []
    la, lb = d_ab.label, d_ba.label
    basis = sorted(set(d_ab.candidate.basis_obs_ids or []) | set(d_ba.candidate.basis_obs_ids or []))
    oa, ob = obs_of_node(a_node), obs_of_node(b_node)
    out: List[MemoryOp] = []
    if la == "restates" and lb == "restates":
        if not scope_match or guarded:
            return []               # G-A3-01-v1 / merge guard: never equivalence
        out.append(_op("edge_add", {"relation": "restates", "a": a_node, "b": b_node, "directed": False,
                                    "status": "provisional", "equivalence": True}, basis, d_ab.ref, "A3",
                       "A3 restates both ways"))
        out.append(_op("group_merge", {"a": oa, "b": ob, "reason": "a3_restates_equivalence"}, basis, d_ab.ref, "A3"))
        return out
    for lab, dd, container, claim in ((la, d_ab, a_node, b_node), (lb, d_ba, b_node, a_node)):
        if lab == "restates" and not guarded:
            out.append(_op("mark_add", {"mark": "covered_by", "a": claim, "b": container}, basis, dd.ref, "A3",
                           "A3 one-way restates"))
            out.append(_op("group_merge", {"a": obs_of_node(claim), "b": obs_of_node(container),
                                           "reason": "a3_covered_by"}, basis, dd.ref, "A3"))
        elif lab == "generalizes":
            out.append(_op("edge_add", {"relation": "generalizes", "a": container, "b": claim, "directed": True,
                                        "status": "provisional"}, basis, dd.ref, "A3", "A3 generalizes"))
    return out


# ---------------------------------------------------------------------------
# B1 (with premises, B3 folded in)
# ---------------------------------------------------------------------------
def consume_b1(d: Decision, claim: Claim, issues_from_insufficient_conclusion: bool = True) -> List[MemoryOp]:
    label = d.label
    if B1_STATUS.get(label) is None or claim is None:
        return []
    ev = b1_evidence(d.candidate, claim.obs_id)
    meta = d.candidate.meta or {}
    # the claim author's own implementing edits and program-checked supporting runs are never shown as
    # counter-evidence
    not_counter = set(meta.get("implementing_edit_ids") or []) | set(meta.get("direct_support_ids") or [])
    counter = [e for e in ev if e not in not_counter] if label in ("refutes", "both") else list(ev)
    reason = f"B1 {label}"
    if label in ("refutes", "both") and not counter and d.provider != "manual":
        # a refutation must carry its counter-evidence; when the only evidence implements / supports the
        # claim, there is none -> not a dispute
        label, reason = ("supports" if meta.get("direct_support_ids") else "insufficient"), \
            f"B1 {label} without counter-evidence -> {'supports' if meta.get('direct_support_ids') else 'insufficient'}"
    if d.gate:
        reason += f" (gate {d.gate}, model said {d.model_label})"
    status = B1_STATUS[label]
    if label == "supports" and d.gate == WEAK_SUPPORT_GATE:
        status = "weak_support"
    basis = sorted(set(d.candidate.basis_obs_ids or []) | set(ev) | {claim.obs_id})
    target = {"claim_id": claim.claim_id, "status": status, "label": label,
              "support_ids": ev if label in ("supports", "both") else [],
              "counter_ids": counter if label in ("refutes", "both", "outdated") else [],
              "rule_id": d.rule_id, "provider": d.provider}
    out = [_op("claim_status_set", target, basis, d.ref, "B1", reason)]
    if label == "both":
        out.append(_op("issue_open", {"issue_id": issue_id_for("disputed_claim", "claim:" + claim.claim_id),
                                      "kind": "disputed_claim", "title": "Disputed: " + claim.text,
                                      "claim_id": claim.claim_id, "status": "disputed"}, basis, d.ref, "B1",
                       "B1 both -> disputed issue"))
    elif label == "insufficient" and claim.claim_class == "conclusion" and issues_from_insufficient_conclusion:
        out.append(_op("issue_open", {"issue_id": issue_id_for("unverified_conclusion", "claim:" + claim.claim_id),
                                      "kind": "unverified_conclusion", "title": "Unverified: " + claim.text,
                                      "claim_id": claim.claim_id}, basis, d.ref, "B1",
                       "B1 insufficient conclusion -> issue"))
    return out

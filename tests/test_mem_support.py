"""memory test support: an in-memory StoreAPI fake (core-independent) and builders for synthetic records.

Imported by the other tests/test_mem_*.py files. It also holds one smoke test so pytest collects it cleanly."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import hearmemory.interfaces as I  # noqa: E402

BASE = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
COMMIT_A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
COMMIT_B = "3f9e0aa1b2c3d4e5f60718293a4b5c6d7e8f9012"


def ts(minutes: float = 0) -> str:
    return (BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeStore:
    """StoreAPI in memory. Raw lists are append-only; iter_* dedupe by id (first wins) like core."""

    def __init__(self, initialised: bool = True, root=None):
        self.root = Path(root or tempfile.mkdtemp(prefix="hearmemory-mem-"))
        self.hearmemory_dir = self.root / ".hearmemory"
        if initialised:
            (self.hearmemory_dir / "locks").mkdir(parents=True, exist_ok=True)
            (self.hearmemory_dir / "state").mkdir(parents=True, exist_ok=True)
            (self.hearmemory_dir / "VERSION").write_text(I.STORE_FORMAT + "\n")
        self._obs = []            # (start_offset, end_offset, obs)
        self._size = 0
        self.claims, self.candidates, self.judgments, self.events = [], [], [], []
        self.state = {}
        self.state_writes = []
        self.window_calls = 0

    # raw -----------------------------------------------------------------------------------------
    def append_observations(self, obs):
        ids = []
        for o in obs:
            line = json.dumps(o.to_dict(), ensure_ascii=False) + "\n"
            n = len(line.encode("utf-8"))
            self._obs.append((self._size, self._size + n, o))
            self._size += n
            ids.append(o.id)
        return ids

    def iter_observations(self, since_offset: int = 0):
        seen = set()
        for start, _end, o in self._obs:
            if o.id in seen:
                continue
            seen.add(o.id)
            if start >= since_offset:
                yield start, o

    def read_observations_window(self, from_offset, max_bytes):
        self.window_calls += 1
        out, end, seen = [], from_offset, set()
        for start, e, o in self._obs:
            if start < from_offset:
                seen.add(o.id)
                continue
            if e - from_offset > max_bytes:
                break
            end = e
            if o.id not in seen:
                seen.add(o.id)
                out.append(o)
        return end, out

    def obs_size(self):
        return self._size

    def _dedupe(self, items, key):
        seen = set()
        for it in items:
            k = key(it)
            if k in seen:
                continue
            seen.add(k)
            yield it

    def append_claims(self, cs):
        self.claims.extend(cs)
        return len(cs)

    def iter_claims(self):
        return self._dedupe(self.claims, lambda c: c.claim_id)

    def append_candidates(self, cs):
        self.candidates.extend(cs)
        return len(cs)

    def iter_candidates(self):
        return self._dedupe(self.candidates, lambda c: c.candidate_id)

    def append_judgments(self, js):
        self.judgments.extend(js)
        return len(js)

    def iter_judgments(self):
        return self._dedupe(self.judgments, lambda j: j.judgment_id)

    def append_events(self, evs):
        self.events.extend(evs)
        return len(evs)

    def iter_events(self):
        return self._dedupe(self.events, lambda e: e.id)

    # state ---------------------------------------------------------------------------------------
    def read_state(self, name):
        d = self.state.get(name)
        return copy.deepcopy(d) if d is not None else None

    def write_state(self, name, data):
        if not self.is_initialised():
            raise FileNotFoundError(".hearmemory is not initialised")
        self.state[name] = json.loads(json.dumps(data))
        self.state_writes.append(name)

    def fingerprint(self):
        parts = [self._size, len(self.claims), len(self.candidates), len(self.judgments), len(self.events)]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]

    def is_initialised(self):
        return (self.hearmemory_dir / "VERSION").exists()

    # convenience -----------------------------------------------------------------------------------
    def add(self, *records):
        for r in records:
            if isinstance(r, I.Observation):
                self.append_observations([r])
            elif isinstance(r, I.Claim):
                self.append_claims([r])
            elif isinstance(r, I.Candidate):
                self.append_candidates([r])
            elif isinstance(r, I.Judgment):
                self.append_judgments([r])
            elif isinstance(r, I.ControlEvent):
                self.append_events([r])
            else:
                raise TypeError(type(r))
        return self


# ---------------------------------------------------------------------------------------------------
# record builders
# ---------------------------------------------------------------------------------------------------
def prov(host="claude", session="s-claude-1", sub=None, sub_type=None, commit=COMMIT_A, source=None, label=None):
    return I.Provenance(host=host, session_id=session, subagent_id=sub, subagent_type=sub_type, agent_label=label,
                        git_branch="main", git_commit=commit, git_dirty=False, cwd=".",
                        source=source or ("import:codex_rollout" if host == "codex" else "hook:PostToolUse"))


CLAUDE = dict(host="claude", session="s-claude-1")
CLAUDE_SUB = dict(host="claude", session="s-claude-1", sub="agent-7", sub_type="explorer")
CODEX = dict(host="codex", session="019f36b5-codex")
CURSOR = dict(host="cursor", session="conv-42")


def obs(key, kind, text, p, minutes, tool=None, paths=None, refs=(), meta=None, excluded=False):
    if tool is None and paths:
        meta = dict(meta or {})
        meta["paths"] = list(paths)
    return I.Observation(id=I.obs_id_for(key), ts=ts(minutes), kind=kind, event_key=key, provenance=p, text=text,
                         tool=tool, text_sha256=I.sha256_text(text), refs=list(refs), meta=dict(meta or {}),
                         excluded=excluded)


def run_obs(key, p, minutes, target="pytest tests/test_sync.py", failed=0, passed=3, failed_ids=(), paths=(),
            output=None):
    status = "error" if failed else "ok"
    code = 1 if failed else 0
    summary = I.RunnerSummary(runner="pytest", passed=passed, failed=failed, failed_ids=list(failed_ids),
                              target=target)
    text = output or (f"$ {target}\n" + "".join(f"FAILED {f} - KeyError: 'tz'\n" for f in failed_ids) +
                      f"{failed} failed, {passed} passed in 0.{int(minutes) % 97:02d}s" if failed else
                      f"$ {target}\n{passed} passed in 0.{int(minutes) % 97:02d}s")
    tool = I.ToolInfo(name="Bash", command=target, paths=list(paths), exit_code=code, status=status, test=summary)
    return obs(key, "command", text, p, minutes, tool=tool)


def edit_obs(key, p, minutes, path, text="@@ -1 +1 @@\n-old\n+new"):
    tool = I.ToolInfo(name="Edit", paths=[path], status="ok")
    return obs(key, "file_edit", text, p, minutes, tool=tool)


def mention(kind, surface, norm, obs_id, span=(0, 0), resolved=()):
    return I.Mention(kind=kind, surface=surface, norm=norm, obs_id=obs_id, span=list(span), grounded=True,
                     resolved=list(resolved))


def claim(o, text, cls="conclusion", paths=(), mentions=(), parent=None, explicit=False):
    start = o.text.find(text)
    span = [max(0, start), max(0, start) + len(text)] if start >= 0 else [0, len(text)]
    return I.Claim(claim_id=I.claim_id_for(o.id, span), obs_id=o.id, span=span, text=text, claim_class=cls,
                   mentions=list(mentions), paths=list(paths), parent_claim_id=parent, explicit=explicit)


def cand_b1(c, evidence_ids, minutes, supersedes=None, extra_state=None):
    state = {"target_claim": c.text, "target_scope": {"project": "demo"},
             "evidence": [{"text": e} for e in evidence_ids], **(extra_state or {})}
    h = I.input_hash("B1", I.TEMPLATE_VERSIONS["B1"], state)
    sk = "claim:" + c.claim_id
    return I.Candidate(candidate_id=I.candidate_id_for("B1", I.TEMPLATE_VERSIONS["B1"], sk, None, h),
                       template_id="B1", template_version=I.TEMPLATE_VERSIONS["B1"], subject_key=sk, state=state,
                       input_hash=h, basis_obs_ids=[c.obs_id] + list(evidence_ids), created_ts=ts(minutes),
                       supersedes=supersedes, meta={"claim_id": c.claim_id, "evidence_obs_ids": list(evidence_ids)})


def cand_pair(tid, node_a, node_b, minutes, direction=None, meta=None):
    state = {"record_a": {"text": node_a}, "record_b": {"text": node_b}, "trusted_context": {"project": "demo"}}
    h = I.input_hash(tid, I.TEMPLATE_VERSIONS[tid], state)
    sk = "pair:" + node_a + "|" + node_b
    m = {"node_a": node_a, "node_b": node_b, **(meta or {})}
    return I.Candidate(candidate_id=I.candidate_id_for(tid, I.TEMPLATE_VERSIONS[tid], sk, direction, h),
                       template_id=tid, template_version=I.TEMPLATE_VERSIONS[tid], subject_key=sk, state=state,
                       input_hash=h, basis_obs_ids=sorted({node_a.split("#")[0], node_b.split("#")[0]}),
                       created_ts=ts(minutes), direction=direction, meta=m)


def cand_a3(ca, cb, minutes, direction, gates=()):
    a = I.span_node(ca.obs_id, ca.span)
    b = I.span_node(cb.obs_id, cb.span)
    return cand_pair("A3", a, b, minutes, direction=direction,
                     meta={"claim_a": ca.claim_id, "claim_b": cb.claim_id, "gates": list(gates)})


_JSEQ = [0]


def judg(cand, label, minutes, provider="jev", rule_id=None, outcome="valid"):
    _JSEQ[0] += 1
    jid = I.stable_id("j-", cand.candidate_id, label, minutes, provider, outcome, _JSEQ[0])
    return I.Judgment(judgment_id=jid, candidate_id=cand.candidate_id, template_id=cand.template_id,
                      template_version=cand.template_version, input_hash=cand.input_hash, provider=provider,
                      outcome=outcome, ts=ts(minutes), label=label if outcome == "valid" else None, rule_id=rule_id)


def event(kind, target, minutes, data=None, p=None):
    eid = I.stable_id("ev-", kind, target, minutes, json.dumps(data or {}, sort_keys=True))
    return I.ControlEvent(id=eid, ts=ts(minutes), kind=kind, target=target, data=dict(data or {}), provenance=p)


def link(obs_id, p, minutes):
    return event("provenance_link", obs_id, minutes, p=p)


def ctx(host="claude", session="s-claude-1", sub=None, paths=(), identifiers=(), query=""):
    return I.AgentContext(host=host, session_id=session, subagent_id=sub, paths=list(paths),
                          identifiers=list(identifiers), query_text=query)


class SupportSmoke(unittest.TestCase):
    def test_fake_store_satisfies_store_protocol(self):
        st = FakeStore()
        self.assertIsInstance(st, I.StoreAPI)
        o = obs("k1", "note", "hello", prov(), 0)
        st.add(o, o)
        self.assertEqual([x.id for _off, x in st.iter_observations()], [o.id])
        end, got = st.read_observations_window(0, 10_000)
        self.assertEqual([x.id for x in got], [o.id])
        self.assertEqual(end, st.obs_size())

"""Claims: assertive sentences from agent-written observations that are about THIS project's code.

Only assistant_message / subagent_result / note / claim observations yield claims; tool output and plans
never do. Lead-in sentences and log fragments are never taken as claims.
"""
from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from hearmemory.testcmd import canonical_target
from hearmemory import interfaces as I
from hearmemory.judge import _compat as C
from hearmemory.judge.mentions import LOG_LINE_RE, Hit

SEG_RE = re.compile(r"(?:[^。！？!?；;\n.]|\.(?!\s|$))+[。！？!?；;.]*")        # sentence segments
CODE_FENCE_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")
LIST_MARK_RE = re.compile(r"^(?:[-*+•]\s+|\d+[.)]\s+|>\s+)")
QUESTION_RE = re.compile(r"[?？]|吗|(?i:^(?:how|what|why|which|whether|should|could|can|would|is it|do we|does it)\b)")
REQUEST_RE = re.compile(r"(?i)^(?:please|let'?s|let me|let us|i will|i'll|i am going to|i'm going to|we will|we'll|"
                        r"we need to|i need to|we should|i should|next|todo|to-do|going to|now,? i|first,? i|then,? i)\b"
                        r"|^(?:请|麻烦|我来|接下来|下一步|我将|我会|让我|我们来|待办)")
HEADING_RE = re.compile(r"^(?:#{1,6}\s|\*\*[^*\n]{1,80}\*\*\s*[:：]?\s*$|[A-Z][\w /-]{0,40}:\s*$)")
ASSERT_RE = re.compile(
    r"(?i)\b(?:is|are|was|were|has|have|had|does|did|fails?|failed|failing|pass(?:es|ed)?|returns?|returned|"
    r"causes?|caused|breaks?|broke|broken|fix(?:es|ed)|works?|worked|throws?|threw|raises?|raised|uses?|used|"
    r"depends on|means|because|due to|no longer|not|never|always|cannot|can't|doesn't|isn't|won't|didn't|"
    r"contains?|drops?|dropped|ignores?|ignored|missing|succeeds?|succeeded|reads?|writes?|skips?|skipped)\b"
    r"|是|不是|导致|因为|由于|已修复|修复了|通过|失败|没有|不会|使用|依赖|返回|报错|引起|说明")
CONCLUSION_RE = re.compile(r"(?i)\b(?:root cause|the (?:real |actual )?(?:bug|issue|problem|cause) (?:is|was)|"
                           r"fixed|resolved|verified|confirmed|is done)\b|根因|已修复|修复了|问题在于|原因是|已解决|已验证")
FAIL_RE = re.compile(r"(?i)\b(?:fail(?:s|ed|ing|ures?)?|broken|breaks|broke|crash(?:es|ed)?|errors? out)\b"
                     r"|\b(?:is|are|stays?|went|turned)\s+red\b|失败|报错|挂了|不通过|没通过|没过")
PASS_RE = re.compile(r"(?i)\b(?:pass(?:es|ed|ing)?|green|succeed(?:s|ed)?|works|working)\b|通过|跑通|能用")
# "3 passed, 0 failed ... no errors" -- a zero count of failures is not a failure
ZERO_COUNT_RE = re.compile(r"(?i)(?:\b0|\bno|\bzero|\bwithout|\bnone)\s*$")
# English test-result language names a run even without a command / test file ("3 passed, 0 failed",
# "all tests pass", "test suite is green"); Chinese "测试全部通过" / "3 个测试" as before
TEST_RESULT_RE = re.compile(
    r"(?i)\b\d+\s+(?:tests?\s+)?(?:passed|failed)\b|\b(?:all\s+)?(?:\d+\s+)?(?:unit\s+)?tests?\s+"
    r"(?:pass(?:es|ed|ing)?|fail(?:s|ed|ing)?|(?:are|were|is)\s+(?:passing|failing|green|red))\b|\btest\s+suite\b"
    r"|测试(?:全部|均|都)?(?:通过|失败)|\d+\s*个测试")
# what is left of a claim once its test-result wording is removed: nothing -> it only restates a run
_RESULT_WORDS_RE = re.compile(
    r"(?i)\b(?:\d+(?:\.\d+)?s?|pytest|py\.test|python3?|unittest|jest|vitest|tests?|suite|runs?|ran|passed|"
    r"pass(?:es|ing)?|failed|fail(?:s|ing|ures?)?|errors?|skipped|warnings?|green|red|all|no|zero|none|and|with|"
    r"in|is|are|was|were|the|result|results|ok|total|seconds?|now|still)\b"
    r"|测试|全部|均|都|已|了|通过|失败|结果|运行|用例|耗时|个|条|秒|共")
NEG_RE = re.compile(r"(?i)(?:\b(?:no longer|not|never|doesn't|don't|didn't|isn't|won't|can't)\b|n't\b|不再|不|没有?|未)\s*\S*\s*$")
PREMISE_RE = re.compile(r"(?i)\b(?:because(?: of)?|since|due to)\b|由于|因为")
COMMAND_TARGET_RE = re.compile(r"`((?:python -m )?(?:pytest|npm test|npm run test|yarn test|pnpm test|go test|"
                               r"cargo test|jest|vitest|make test)[^`]*)`")

# agents (especially writing Chinese) name the command WITHOUT backticks: "运行 python -m pytest -q
# 测试通过（1 passed）". Only flag / path-like arguments are kept, so "pytest passes" is not a target "passes".
BARE_COMMAND_TARGET_RE = re.compile(
    r"(?<![\w`/.-])((?:python3? -m )?(?:pytest|py\.test|npm test|npm run test|yarn test|pnpm test|go test|"
    r"cargo test|jest|vitest|make test)(?:[ \t]+(?:-[\w-]+(?:=[^\s`]+)?|[A-Za-z0-9_.-]*/[A-Za-z0-9_./:\[\]-]*"
    r"|[A-Za-z0-9_.-]+::?[A-Za-z0-9_./:\[\]-]+|[\w-]+\.py))*)"
    r"(?![\w-])")

# Agents quote hearmemory's own output back -- the SessionStart brief, hearmemory_recall results,
# lines carrying a status tag ("- `[SUPPORTED]` <claim> — codex · ...", "标记为 `[INSUFFICIENT]`") or a hearmemory
# obs / claim id. Re-extracting those made memory look like new, independent findings. Such lines, and a whole
# block that starts with hearmemory's own header line ("[hearmemory] ...", "[hearmemory recall] ...") up to the next blank
# line, are masked before sentences are cut.
_ECHO_TAGS = sorted({t for tags in I.STATUS_TAGS.values() for t in tags.values()}, key=len, reverse=True)
ECHO_LINE_RE = re.compile("|".join(re.escape(t) for t in _ECHO_TAGS) + r"|\b[oc]-[0-9a-f]{16}\b")
ECHO_HEADER_RE = re.compile(r"^[\s>*_`#-]*" + re.escape(I.HEARMEMORY_OUTPUT_MARKER) + r"[\] ]")

MIN_LEN, MAX_LEN = 12, 400
CLASS_PRIORITY = {"conclusion": 0, "status": 1, "premise": 2, "other": 3}


def claimed_outcome(text: str) -> Optional[str]:
    """'pass' / 'fail' when the sentence asserts exactly one run outcome (negation-aware), else None."""
    fails: List[Tuple[int, int]] = []
    passes: List[Tuple[int, int]] = []
    for m in FAIL_RE.finditer(text or ""):
        if ZERO_COUNT_RE.search(text[max(0, m.start() - 10):m.start()]):
            continue                                    # "0 failed", "no failures"
        neg = NEG_RE.search(text[max(0, m.start() - 16):m.start()])
        (passes if neg and m.group(0) not in ("不通过", "没通过", "没过") else fails).append(m.span())
    for m in PASS_RE.finditer(text or ""):
        if any(a <= m.start() < b for a, b in fails):
            continue
        neg = NEG_RE.search(text[max(0, m.start() - 16):m.start()])
        (fails if neg else passes).append(m.span())
    if fails and not passes:
        return "fail"
    if passes and not fails:
        return "pass"
    return None


def command_targets(text: str) -> List[str]:
    """Test commands named in a claim (backticked, or bare with flag/path arguments only), normalised like
    RunnerSummary.target."""
    text = text or ""
    out = [normalize_command(m.group(1)) for m in COMMAND_TARGET_RE.finditer(text)]
    masked = re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), text)
    out += [normalize_command(m.group(1)) for m in BARE_COMMAND_TARGET_RE.finditer(masked)]
    return C.uniq(t for t in out if t)


def normalize_command(cmd: str) -> str:
    """The shared canonical test target (hearmemory.testcmd): drop noise flags and wrappers, keep
    paths and -k/-m expressions, unify `python -m pytest` / `.venv/bin/pytest` / `pytest`."""
    return canonical_target(cmd)


def _is_log_or_code(seg: str) -> bool:
    if seg.startswith("$ ") or seg.startswith(">>> "):
        return True
    if LOG_LINE_RE.search(seg):
        return True
    chars = [ch for ch in seg if not ch.isspace()]
    if not chars:
        return True
    non_alnum = sum(1 for ch in chars if not (ch.isalnum() or ch in "_"))
    return non_alnum / float(len(chars)) >= 0.6


def classify(text: str, hits: Sequence[Hit]) -> str:
    if CONCLUSION_RE.search(text):
        return "conclusion"
    has_target = any(h.mention.kind == "test" for h in hits) or bool(command_targets(text)) or any(
        h.mention.kind == "file" and ("test" in h.mention.surface) for h in hits) or bool(TEST_RESULT_RE.search(text))
    if has_target and claimed_outcome(text):
        return "status"
    return "other"


def restates_run_only(text: str) -> bool:
    """the claim says nothing but a test result ("pytest run: 3 passed, 0 failed (in 0.00s), no
    errors. Test suite is green.") -- in a brief it collapses into the run line it restates."""
    if claimed_outcome(text) is None:
        return False
    rest = _RESULT_WORDS_RE.sub(" ", text or "")
    return not [w for w in re.split(r"[^\w一-鿿]+", rest) if len(w) >= 2 and not w.isdigit()]


def _segments(text: str) -> List[Tuple[int, int]]:
    masked = CODE_FENCE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    # inline code keeps its content (paths / names inside backticks are the most useful mentions)
    out = []
    for m in SEG_RE.finditer(masked):
        a, b = m.span()
        if not masked[a:b].strip():
            continue
        seg = text[a:b]
        lead = len(seg) - len(seg.lstrip())
        a += lead
        seg = text[a:b]
        lm = LIST_MARK_RE.match(seg)
        if lm:
            a += lm.end()
        seg = text[a:b].rstrip()
        b = a + len(seg)
        if b > a:
            out.append((a, b))
    return out


def mask_hearmemory_echo(text: str) -> str:
    """`text` with every line that is hearmemory output (see ECHO_LINE_RE / ECHO_HEADER_RE) blanked out; same length
    and newlines, so spans still index the original text."""
    if not text or not (ECHO_LINE_RE.search(text) or I.HEARMEMORY_OUTPUT_MARKER in text):
        return text
    out: List[str] = []
    in_block = False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if not body.strip():
            in_block = False
        elif ECHO_HEADER_RE.match(body):
            in_block = True
        if in_block or ECHO_LINE_RE.search(body):
            out.append(" " * len(body) + line[len(body):])
        else:
            out.append(line)
    return "".join(out)


def _hits_in(hits: Sequence[Hit], a: int, b: int) -> List[Hit]:
    return [h for h in hits if a <= h.mention.span[0] and h.mention.span[1] <= b]


def _paths_of(hits: Sequence[Hit]) -> List[str]:
    out = []
    for h in hits:
        m = h.mention
        if m.grounded and m.kind in ("file", "module") and len(m.resolved) == 1:
            out.append(m.resolved[0])
        elif m.grounded and m.kind == "test" and len(m.resolved) == 1:
            out.append(m.resolved[0].split("::", 1)[0])
    return C.uniq(out)


def _make(obs_id: str, text: str, a: int, b: int, cls: str, hits: Sequence[Hit], explicit: bool = False,
          parent: Optional[str] = None) -> I.Claim:
    inner = _hits_in(hits, a, b)
    return I.Claim(claim_id=I.claim_id_for(obs_id, [a, b]), obs_id=obs_id, span=[a, b], text=text[a:b][:MAX_LEN],
                   claim_class=cls, mentions=[h.mention for h in inner], paths=_paths_of(inner),
                   parent_claim_id=parent, explicit=explicit)


def _premise(obs_id: str, text: str, a: int, b: int, hits: Sequence[Hit], parent_id: str) -> Optional[I.Claim]:
    m = PREMISE_RE.search(text, a, b)
    if not m:
        return None
    pa = m.end()
    while pa < b and text[pa] in " :,，：":
        pa += 1
    pb = b
    while pb > pa and text[pb - 1] in " .。;；!！":
        pb -= 1
    if pb - pa < 10 or not any(h.mention.grounded for h in _hits_in(hits, pa, pb)):
        return None
    return _make(obs_id, text, pa, pb, "premise", hits, parent=parent_id)


def extract_claims(obs: I.Observation, hits: Sequence[Hit], cfg: Optional[Mapping[str, Any]] = None) -> List[I.Claim]:
    """Claims of one assertive observation, at most extract.claims_per_message, priority
    conclusion > status > premise > other; premises carry parent_claim_id (B3 folded into B1)."""
    if obs.kind not in I.ASSERTIVE_OBS_KINDS or obs.excluded:
        return []
    text = obs.text or ""
    cap = int(C.cfg_get(cfg, "extract", "claims_per_message", 5))
    out: List[I.Claim] = []
    if obs.kind == "claim":
        a = len(text) - len(text.lstrip())
        b = len(text.rstrip())
        if b - a <= 0:
            return []
        b = min(b, a + MAX_LEN)
        c = _make(obs.id, text, a, b, classify(text[a:b], _hits_in(hits, a, b)), hits, explicit=True)
        out.append(c)
        p = _premise(obs.id, text, a, b, hits, c.claim_id)
        if p:
            out.append(p)
        return out[:cap]
    for a, b in _segments(mask_hearmemory_echo(text)):
        seg = text[a:b]
        if not (MIN_LEN <= len(seg) <= MAX_LEN):
            continue
        if QUESTION_RE.search(seg) or REQUEST_RE.search(seg) or HEADING_RE.search(seg):
            continue
        if seg.endswith((":", "：")) or _is_log_or_code(seg):
            continue
        if not ASSERT_RE.search(seg):
            continue
        inner = _hits_in(hits, a, b)
        if not any(h.mention.grounded for h in inner):
            continue
        c = _make(obs.id, text, a, b, classify(seg, inner), hits)
        out.append(c)
        p = _premise(obs.id, text, a, b, hits, c.claim_id)
        if p:
            out.append(p)
    out.sort(key=lambda c: (CLASS_PRIORITY.get(c.claim_class, 9), c.span[0]))
    kept = out[:cap]
    kept_ids = {c.claim_id for c in kept}
    kept = [c for c in kept if not c.parent_claim_id or c.parent_claim_id in kept_ids]
    kept.sort(key=lambda c: (c.span[0], c.span[1]))
    return kept

"""Rendering helpers shared by brief / check / recall (en + zh).

Relative times ("2h ago") are computed HERE, at render time only; they never enter model-visible state
. Provenance format: host · session <8> · [subagent <type|id>] · <age> · <commit 7>."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from hearmemory.interfaces import STATUS_TAGS

from .text import clip, ts_seconds

TEXT = {
    "en": {
        "brief_header": "[hearmemory] Shared project memory — {n} items{pending}{asof}. Format: status · claim · source.",
        "pending": " ({k} judgments pending)",
        "asof": " (memory as of {ago})",
        "p1": "Refuted / disputed:",
        "p2": "Open issues:",
        "p3": "New from other agents:",
        "brief_footer": "Details: hearmemory_recall (MCP) or `hearmemory recall <query>`. Before committing: hearmemory_check / "
                        "`hearmemory check --staged`.",
        "counter": "counter-evidence",
        "colon": ": ",
        "support": "evidence",
        "suggested": "suggested check",
        "premise": "premise unsupported",
        "may_same_object": "(may be the same object)",
        "same_object": "(same object)",
        "likely_same_event": "(likely the same occurrence)",
        "same_event": "(same occurrence)",
        "different_objects": "(different objects with similar names)",
        "different_events": "(different occurrences)",
        "restated": "(+{n} restatement(s) from {src})",
        "reported": "(the agent's own report)",
        "addressed_by": "→ {who} edited {paths}{rest}",
        "session_word": "session",
        "run_line": "test `{target}` {outcome}{summary}",
        "passed": "passed", "failed": "failed",
        "check_header_git_commit": "[hearmemory pre-commit check] You are about to commit (not executed yet). "
                                   "{n} memory items may matter:",
        "check_header_claim": "[hearmemory check] Before relying on this claim: {n} memory items may matter:",
        "check_header_finish": "[hearmemory check] Before finishing: {n} memory items may matter:",
        "check_hold": "Run the same command again to commit anyway, or change it first.",
        "check_block": "Resolve the items or run 'hearmemory check --ack <key>' (keys: {keys}).",
        "check_warn": "(Warning only: nothing was blocked.)",
        "failing": "[FAILING] `{target}` last run failed{summary}",
        "new": "[NEW]",
        "recall_header": "[hearmemory recall] {q}{n} results{pending}{asof}.",
        "recall_empty": "[hearmemory recall] no matching memory{pending}{asof}.",
        "related": "related",
        "archived_hint": "",
        "author_you": "you",
    },
    "zh": {
        "brief_header": "[hearmemory] 项目共享记忆 — {n} 条{pending}{asof}。格式：状态 · 说法 · 来源。",
        "pending": "（{k} 条判断未完成）",
        "asof": "（记忆截至 {ago}）",
        "p1": "已被反驳 / 有争议：",
        "p2": "未决问题：",
        "p3": "其他 agent 的新发现：",
        "brief_footer": "详情：hearmemory_recall（MCP）或 `hearmemory recall <关键词>`。提交前：hearmemory_check / `hearmemory check --staged`。",
        "counter": "反证",
        "colon": "：",
        "support": "证据",
        "suggested": "建议检查",
        "premise": "前提缺证据",
        "may_same_object": "（可能是同一对象）",
        "same_object": "（同一对象）",
        "likely_same_event": "（可能是同一次事件）",
        "same_event": "（同一次事件）",
        "different_objects": "（名字相近但不是同一对象）",
        "different_events": "（不是同一次事件）",
        "restated": "（另有 {n} 条同义说法，来源 {src}）",
        "reported": "（该 agent 自己的说法）",
        "addressed_by": "→ {who} 修改了 {paths}{rest}",
        "session_word": "会话",
        "run_line": "测试 `{target}` {outcome}{summary}",
        "passed": "通过", "failed": "失败",
        "check_header_git_commit": "[hearmemory 提交前检查] 你正要提交（还没有执行）。记忆里有 {n} 条可能相关的提醒：",
        "check_header_claim": "[hearmemory 检查] 依赖这个结论之前，记忆里有 {n} 条可能相关的提醒：",
        "check_header_finish": "[hearmemory 检查] 结束之前，记忆里有 {n} 条可能相关的提醒：",
        "check_hold": "再执行一次同样的命令就会照常提交；也可以先修改。",
        "check_block": "请先处理这些条目，或运行 'hearmemory check --ack <key>'（key：{keys}）。",
        "check_warn": "（只是提醒，没有拦截。）",
        "failing": "[未通过] `{target}` 最近一次运行失败{summary}",
        "new": "[新]",
        "recall_header": "[hearmemory 召回] {q}{n} 条结果{pending}{asof}。",
        "recall_empty": "[hearmemory 召回] 没有匹配的记忆{pending}{asof}。",
        "related": "相关",
        "archived_hint": "",
        "author_you": "你",
    },
}


def lang_of(lang: Optional[str]) -> str:
    return lang if lang in TEXT else "en"


def t(lang: str, key: str, **kw: Any) -> str:
    s = TEXT[lang_of(lang)][key]
    return s.format(**kw) if kw else s


def tag(status: str, lang: str = "en") -> str:
    return STATUS_TAGS[lang_of(lang)].get(status, "[" + status.upper() + "]")


def ago(ts: Optional[str], now: Optional[str], lang: str = "en") -> str:
    a, b = ts_seconds(ts), ts_seconds(now)
    if a is None or b is None:
        return "?"
    d = max(0, int(b - a))
    zh = lang_of(lang) == "zh"
    if d < 60:
        return "刚刚" if zh else "just now"
    for unit_s, en, cn in ((86400 * 7, "w", "周"), (86400, "d", "天"), (3600, "h", "小时"), (60, "m", "分钟")):
        if d >= unit_s:
            n = d // unit_s
            return f"{n} {cn}前" if zh else f"{n}{en} ago"
    return "?"


def prov_text(entry: Optional[Mapping[str, Any]], now: Optional[str], lang: str = "en") -> str:
    if not entry:
        return "?"
    parts = [str(entry.get("host") or "?")]
    sess = entry.get("session")
    if sess:
        parts.append(("会话 " if lang_of(lang) == "zh" else "session ") + str(sess)[:8])
    sub = entry.get("subagent_type") or entry.get("subagent")
    if sub:
        parts.append(("子 agent " if lang_of(lang) == "zh" else "subagent ") + str(sub)[:24])
    parts.append(ago(entry.get("ts"), now, lang))
    if entry.get("commit"):
        # a run on a dirty tree is not a run of `commit`; the commit made right after it is shown
        commit = str(entry["commit"])[:7] + ("+dirty" if entry.get("dirty") else "")
        if entry.get("commit_to"):
            commit += " → " + str(entry["commit_to"])[:7]
        parts.append(commit)
    return " · ".join(parts)


def addressed_text(a: Mapping[str, Any], lang: str = "en") -> str:
    """"→ codex session 01a0d44b edited tests/test_calc.py, 11 passed, 1dcbbe0"."""
    who = f"{a.get('host') or '?'} {t(lang, 'session_word')} {str(a.get('session') or '?')[:8]}"
    if a.get("subagent_type"):
        who += " subagent " + str(a["subagent_type"])[:24]
    sep = "，" if lang_of(lang) == "zh" else ", "
    rest = "".join(sep + str(x) for x in (a.get("run"), str(a.get("commit") or "")[:7]) if x)
    return t(lang, "addressed_by", who=who, paths=", ".join((a.get("paths") or [])[:3]), rest=rest)


def quote(text: str, n: int = 160) -> str:
    return '"' + clip(text, n).replace('"', "'") + '"'


def short_issue_id(iid: str) -> str:
    return iid[:6] if iid.startswith("i-") else iid[:6]


def run_text(run: Optional[Mapping[str, Any]], lang: str = "en") -> str:
    if not run:
        return ""
    summ = run.get("summary") or ""
    return t(lang, "run_line", target=clip(run.get("target") or "", 100),
             outcome=t(lang, "passed" if run.get("outcome") == "pass" else "failed"),
             summary=f" ({summ})" if summ else "")


def evidence_text(oid: str, obs_index: Mapping[str, Mapping[str, Any]], now: Optional[str], lang: str = "en",
                  n: int = 140) -> str:
    """One line describing an evidence observation: the run outcome, or the command, or an excerpt."""
    e = obs_index.get(oid)
    if not e:
        return oid
    if e.get("run"):
        body = run_text(e["run"], lang)
    elif e.get("edit_summary"):
        body = clip(str(e["edit_summary"]), max(n, 60))
    elif e.get("command"):
        code = e.get("exit_code")
        body = f"`{clip(e['command'], 90)}`" + (f" exit {code}" if code is not None else "")
    else:
        body = quote(e.get("excerpt") or "", n)
    return f"{body} ({prov_text(e, now, lang)})"

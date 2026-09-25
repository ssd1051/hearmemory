"""Contract tests for hearmemory.interfaces. Stdlib unittest; runs under pytest too."""
import dataclasses
import importlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import hearmemory.interfaces as I  # noqa: E402


def _prov(**kw):
    base = dict(host="claude", session_id="s1", subagent_id="a1", git_branch="main", git_commit="0" * 40,
                cwd=".", source="hook:PostToolUse")
    base.update(kw)
    return I.Provenance(**base)


class ImportHygiene(unittest.TestCase):
    def test_import_has_no_side_effects_and_only_stdlib(self):
        code = ("import sys, os, builtins; opened=[]; real=builtins.open\n"
                "def spy(*a, **k):\n    opened.append(a[0] if a else k.get('file')); return real(*a, **k)\n"
                "builtins.open=spy; sys.path.insert(0, %r)\n"
                "before=set(sys.modules); import hearmemory.interfaces\n"
                "new=set(sys.modules)-before\n"
                "third=[m for m in new if m.split('.')[0] not in sys.stdlib_module_names and m.split('.')[0]!='hearmemory']\n"
                "print(len(opened), third)\n") % str(REPO / "src")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out[0], "0", "importing interfaces must not open files")
        self.assertEqual(out[1], "[]", "interfaces must import only the stdlib")

    def test_version_strings(self):
        for name in ("INTERFACES_VERSION", "STORE_FORMAT", "OBS_SCHEMA", "EXTRACTOR_VERSION"):
            self.assertTrue(getattr(I, name))


class Vocabularies(unittest.TestCase):
    def test_templates_are_the_four(self):
        self.assertEqual(I.TEMPLATE_IDS, ("A1", "A2", "A3", "B1"))
        for t in I.TEMPLATE_IDS:
            self.assertIn(t, I.TEMPLATE_VERSIONS)
            self.assertIn(I.TEMPLATE_UNKNOWN_LABEL[t], I.TEMPLATE_LABELS[t])
        self.assertFalse(set(I.TEMPLATE_IDS) & set(I.DEFERRED_TEMPLATES))
        self.assertFalse(set(I.TEMPLATE_IDS) & set(I.PROGRAM_RULE_TEMPLATES))

    def test_obs_kind_partition(self):
        parts = set(I.PRIMARY_OBS_KINDS) | set(I.ASSERTIVE_OBS_KINDS) | set(I.NON_EVIDENCE_OBS_KINDS)
        self.assertEqual(parts, set(I.OBS_KINDS))
        self.assertFalse(set(I.PRIMARY_OBS_KINDS) & set(I.ASSERTIVE_OBS_KINDS))

    def test_mention_compat_covers_kinds(self):
        self.assertEqual(set(I.MENTION_COMPAT), set(I.MENTION_KINDS))

    def test_blocking_kinds_subset(self):
        self.assertTrue(set(I.BLOCKING_WARNING_KINDS) <= set(I.WARNING_KINDS))
        self.assertTrue(set(I.OPEN_ISSUE_STATUSES) <= set(I.ISSUE_STATUSES))
        self.assertEqual(I.PRECOMMIT_MODES[0], "off")

    def test_hook_events_hosts(self):
        self.assertTrue(set(I.HOOK_EVENTS) <= set(I.HOSTS))
        self.assertTrue(set(I.INSTALLABLE_HOSTS) <= set(I.HOSTS))

    def test_mcp_and_cli_names(self):
        self.assertEqual(len(set(I.MCP_TOOLS)), 5)
        for c in ("init", "status", "record", "recall", "check", "issues", "import", "worker", "doctor",
                  "uninstall"):
            self.assertIn(c, I.CLI_COMMANDS)


class Helpers(unittest.TestCase):
    def test_stable_ids_deterministic(self):
        self.assertEqual(I.obs_id_for("claude:s:t"), I.obs_id_for("claude:s:t"))
        self.assertNotEqual(I.obs_id_for("claude:s:t"), I.obs_id_for("claude:s:u"))
        self.assertTrue(I.obs_id_for("x").startswith("o-"))
        self.assertEqual(len(I.obs_id_for("x")), 18)
        self.assertTrue(I.issue_id_for("manual", "x").startswith("i-"))
        self.assertTrue(I.claim_id_for("o-1", [0, 3]).startswith("c-"))

    def test_input_hash_order_independent_keys(self):
        a = I.input_hash("B1", "hm-B1.1", {"x": 1, "y": [1, 2]})
        b = I.input_hash("B1", "hm-B1.1", {"y": [1, 2], "x": 1})
        self.assertEqual(a, b)
        self.assertNotEqual(a, I.input_hash("B1", "hm-B1.2", {"x": 1, "y": [1, 2]}))

    def test_span_node(self):
        self.assertEqual(I.span_node("o-1", None), "o-1")
        self.assertEqual(I.span_node("o-1", [3, 9]), "o-1#3-9")

    def test_brief_relevance_gate_and_saturation(self):
        self.assertEqual(I.brief_relevance(0, 0), 0.0)
        self.assertEqual(I.brief_relevance(1, 0), 0.6)
        self.assertEqual(I.brief_relevance(0, 1), 0.2)
        self.assertEqual(I.brief_relevance(0, 5), 0.4)
        self.assertEqual(I.brief_relevance(1, 2), 1.0)
        with self.assertRaises(ValueError):
            I.brief_relevance(0.3, 0)

    def test_estimate_usd(self):
        self.assertAlmostEqual(I.estimate_usd(1_000_000), 0.042)
        self.assertEqual(I.estimate_usd(-5), 0.0)


class RoundTrips(unittest.TestCase):
    def _rt(self, obj):
        d = obj.to_dict()
        json.dumps(d)                                   # JSON-able
        back = type(obj).from_dict(json.loads(json.dumps(d)))
        self.assertEqual(back, obj)
        return d

    def test_observation(self):
        o = I.Observation(id=I.obs_id_for("k"), ts="2026-09-24T00:00:00.000000Z", kind="command",
                          event_key="k", provenance=_prov(),
                          tool=I.ToolInfo(name="Bash", command="pytest -q", paths=["tests/test_a.py"],
                                          exit_code=1, status="error",
                                          test=I.RunnerSummary(runner="pytest", failed=1,
                                                               failed_ids=["tests/test_a.py::test_x"])),
                          text="1 failed", text_sha256=I.sha256_text("1 failed"))
        d = self._rt(o)
        self.assertEqual(d["schema"], I.OBS_SCHEMA)
        self.assertTrue(o.is_primary and not o.is_assertive)
        self.assertEqual(o.paths, ["tests/test_a.py"])

    def test_unknown_keys_ignored(self):
        d = I.Provenance(host="codex").to_dict()
        d["future_field"] = 1
        self.assertEqual(I.Provenance.from_dict(d).host, "codex")

    def test_claim_candidate_judgment(self):
        m = I.Mention(kind="file", surface="sync.py", norm="path:src/sync.py", obs_id="o-1", span=[0, 7],
                      grounded=True, resolved=["src/sync.py"])
        c = I.Claim(claim_id=I.claim_id_for("o-1", [0, 20]), obs_id="o-1", span=[0, 20],
                    text="sync.py drops the tz", claim_class="conclusion", mentions=[m], paths=["src/sync.py"])
        self._rt(c)
        st = {"target_claim": c.text, "target_scope": {"project": "p"}, "evidence": []}
        h = I.input_hash("B1", I.TEMPLATE_VERSIONS["B1"], st)
        k = I.Candidate(candidate_id=I.candidate_id_for("B1", "hm-B1.1", "claim:" + c.claim_id, None, h),
                        template_id="B1", template_version="hm-B1.1", subject_key="claim:" + c.claim_id,
                        state=st, input_hash=h, basis_obs_ids=["o-1"], created_ts="t")
        self._rt(k)
        j = I.Judgment(judgment_id="j-1", candidate_id=k.candidate_id, template_id="B1",
                       template_version="hm-B1.1", input_hash=h, provider="jev", outcome="valid", ts="t",
                       label="refutes", probabilities={"refutes": 0.9, "supports": 0.1})
        self._rt(j)
        self._rt(I.ExtractResult(claims=[c], candidates=[k], dropped={"A1_not_grounded": 3}))

    def test_memory_state(self):
        c = I.Claim(claim_id="c-1", obs_id="o-1", span=[0, 3], text="abc", claim_class="status")
        st = I.MemoryState(fingerprint="f", claims={"c-1": I.ClaimView(claim=c, status="refuted",
                                                                        history=[I.HistoryEntry("t", "x", "y")])},
                           edges={"e-1": I.Edge(edge_id="e-1", relation="same_object", a="o-1#0-3", b="o-2",
                                                status="provisional")},
                           marks={"m-1": I.Mark(mark_id="m-1", kind="covered_by", a="o-1")},
                           issues={"i-1": I.Issue(issue_id="i-1", kind="manual", title="t",
                                                  opened_by=I.Provenance(host="cli"))},
                           source_groups={"o-1": "sg-o-1"}, tiers={"o-9": "archive"})
        back = I.MemoryState.from_dict(json.loads(json.dumps(st.to_dict())))
        self.assertEqual(back, st)

    def test_brief_check_manifest(self):
        ctx = I.AgentContext(host="claude", session_id="s", paths=["a.py"])
        self._rt(I.BriefRequest(context=ctx))
        self._rt(I.Brief(text="x", items=[I.BriefItem(tier="P1", kind="claim_status", item_key="k", text="x")]))
        self._rt(I.CheckRequest(context=ctx, payload_text="fix", paths=["a.py"]))
        r = I.CheckResult(decision="warn", warnings=[I.CheckWarning(kind="relies_on_refuted", item_key="k",
                                                                    text="x")])
        self._rt(r)
        self.assertTrue(r.ok)
        self.assertFalse(I.CheckResult(decision="block").ok)
        self._rt(I.RecallResult(query="q", items=[I.RecallItem(obs_id="o", kind="note", excerpt="e", score=1.0,
                                                               provenance_text="p")]))
        self._rt(I.InstallManifest(root="/r", python="/py", created_ts="t",
                                   records=[I.InstallRecord(path=".cursor/mcp.json", action="json_merged",
                                                            host="cursor", json_keys=[["mcpServers", "hearmemory"]])]))
        self._rt(I.HookResult(stdout="{}"))
        self._rt(I.ControlEvent(id="e", ts="t", kind="issue_close", target="i-1", provenance=_prov()))
        self._rt(I.LedgerRow(ts="t", day="2026-09-24", candidate_id=None, template_id=None, outcome="valid"))

    def test_budget_status(self):
        self.assertTrue(I.BudgetStatus(day="d", calls=3, usd=0.0, call_cap=3, usd_cap=1.0).exhausted)
        self.assertFalse(I.BudgetStatus(day="d", calls=0, usd=0.0, call_cap=3, usd_cap=1.0).exhausted)


class ProtocolsAndEntryPoints(unittest.TestCase):
    def test_protocols_runtime_checkable(self):
        class FakeJudge:
            name = "rule"

            def judge(self, cands, deadline_s):
                return []
        self.assertIsInstance(FakeJudge(), I.JudgeAPI)
        self.assertNotIsInstance(object(), I.JudgeAPI)

    def test_entry_points_well_formed(self):
        for key, ref in I.ENTRY_POINTS.items():
            mod, _, attr = ref.partition(":")
            self.assertTrue(mod.startswith("hearmemory.") and attr, key)

    def test_existing_entry_points_importable(self):
        """Entry points whose module exists must expose the named attribute (all of them, in a full install)."""
        for key, ref in I.ENTRY_POINTS.items():
            mod, _, attr = ref.partition(":")
            try:
                m = importlib.import_module(mod)
            except ModuleNotFoundError as e:
                if e.name and (mod == e.name or mod.startswith(e.name + ".")):
                    continue
                raise
            self.assertTrue(hasattr(m, attr), f"{key}: {mod} lacks {attr}")

    def test_all_records_are_dataclasses_with_defaults_ok(self):
        for name in dir(I):
            obj = getattr(I, name)
            if isinstance(obj, type) and issubclass(obj, I.Record) and obj is not I.Record:
                self.assertTrue(dataclasses.is_dataclass(obj), name)


class ConfigAndLayout(unittest.TestCase):
    def test_default_config_values_are_valid(self):
        c = I.DEFAULT_CONFIG
        self.assertTrue(set(c["hosts"]["enabled"]) <= set(I.INSTALLABLE_HOSTS))
        for k in ("claude_mode", "git_mode", "cursor_mode", "codex_mode"):
            self.assertIn(c["precommit"][k], I.PRECOMMIT_MODES)
            self.assertNotIn(c["precommit"][k], ("hold_once", "block"), "blocking must be opt-in")
        self.assertIn(c["brief"]["lang"], I.LANGS)
        self.assertEqual(c["jev"]["model"], I.JEV_MODEL_DEFAULT)
        self.assertAlmostEqual(c["jev"]["usd_per_million_input"], I.JEV_USD_PER_MILLION_INPUT)
        self.assertEqual(c["hooks"]["timeout_ms"], I.HOOK_TIMEOUT_MS_DEFAULT)
        self.assertEqual(c["capture"]["max_text_chars"], I.MAX_TEXT_CHARS_DEFAULT)
        self.assertIn(".env", c["privacy"]["exclude_globs"])

    def test_default_config_is_toml_representable(self):
        def ok(v):
            if isinstance(v, (str, bool, int, float)):
                return True
            return isinstance(v, list) and all(isinstance(x, str) for x in v)
        for sect, kv in I.DEFAULT_CONFIG.items():
            for k, v in kv.items():
                self.assertTrue(ok(v), f"{sect}.{k}")

    def test_layout(self):
        for k in I.RAW_FILES:
            self.assertTrue(I.LAYOUT[k].endswith(".jsonl"), k)
        self.assertTrue(all(v.startswith("state/") for v in I.STATE_FILES.values()))
        self.assertEqual(len(set(I.LOCK_NAMES)), len(I.LOCK_NAMES))

    def test_status_tags_cover_claim_statuses(self):
        for lang in I.LANGS:
            self.assertTrue(set(I.CLAIM_STATUSES) <= set(I.STATUS_TAGS[lang]), lang)


def _roundtrip(tc, obj):
    back = type(obj).from_dict(json.loads(json.dumps(obj.to_dict())))
    tc.assertEqual(back, obj)
    return back


class RevisionOne(unittest.TestCase):
    """Pins contract details of interfaces version 1.1."""

    def test_claude_failure_event_registered(self):
        self.assertIn("PostToolUseFailure", I.HOOK_EVENTS["claude"])
        self.assertEqual(I.CLAUDE_CAPTURE_EVENTS, ("PostToolUse", "PostToolUseFailure"))
        self.assertTrue(set(I.CLAUDE_CAPTURE_EVENTS) <= set(I.HOOK_EVENTS["claude"]))
        tools = I.CLAUDE_TOOL_MATCHER.split("|")
        for name in ("Bash", "Edit", "Write", "Task", "mcp__hearmemory__hearmemory_record"):
            self.assertIn(name, tools)

    def test_every_hook_event_has_a_profile_and_budgets_fit(self):
        for host, events in I.HOOK_EVENTS.items():
            for ev in events:
                prof = I.HOOK_PROFILES.get(f"{host}:{ev}")
                self.assertIn(prof, I.HOOK_STEP_BUDGETS_MS, f"{host}:{ev}")
        self.assertEqual(set(I.HOOK_PROFILE_TOTAL_KEY), set(I.HOOK_STEP_BUDGETS_MS))
        for prof, steps in I.HOOK_STEP_BUDGETS_MS.items():
            self.assertIn("startup", steps, prof)
            self.assertTrue(I.hook_budget_fits(prof, I.DEFAULT_CONFIG["hooks"]), prof)
        for prof in ("push", "precommit", "session_start"):
            self.assertIn("render", I.HOOK_STEP_BUDGETS_MS[prof], "render must be reserved")
        self.assertFalse(I.hook_budget_fits("precommit", {"timeout_ms": 1000}))
        self.assertFalse(I.hook_budget_fits("session_start", {}))

    def test_pipeline_lock_and_extract_index(self):
        self.assertIn("pipeline", I.LOCK_NAMES)
        self.assertEqual(I.STATE_FILES["extract_index"], "state/extract_index.json")

    def test_actor_keys(self):
        cur1 = I.Provenance(host="cursor", session_id="conv-1", source="hook:afterShellExecution")
        cur2 = I.Provenance(host="cursor", session_id="conv-1", subagent_id="gen-2", source="hook:afterFileEdit")
        self.assertEqual(I.actor_key(cur1), I.actor_key(cur2), "generation id must not split a Cursor actor")
        main = I.Provenance(host="claude", session_id="s", source="hook:PostToolUse")
        sub = I.Provenance(host="claude", session_id="s", subagent_id="a1", source="hook:PostToolUse")
        self.assertEqual(I.actor_key(main), "claude:s:main")
        self.assertNotEqual(I.actor_key(main), I.actor_key(sub))
        self.assertEqual(I.actor_key(I.Provenance(host="codex", session_id="u1", source="import:codex_rollout")),
                         "codex:u1")
        self.assertEqual(I.actor_key(I.Provenance(host="codex", session_id="codex-1-2", source="cli")), "codex:?")
        self.assertEqual(I.actor_key(I.Provenance(host="claude", session_id="mcp-9-1", source="mcp")), "claude:?")
        self.assertTrue(I.actors_may_coincide("codex:?", "codex:u1"))
        self.assertTrue(I.actors_may_coincide("claude:s:a1", "claude:?"))
        self.assertFalse(I.actors_may_coincide("codex:?", "claude:s:main"))
        self.assertFalse(I.actors_may_coincide("codex:u1", "codex:u2"))
        self.assertFalse(I.actors_may_coincide("claude:s:main", "claude:s:a1"))

    def test_actor_map_links(self):
        rec = I.Observation(id="o-1", ts="t", kind="claim", event_key="cli:x",
                            provenance=I.Provenance(host="claude", session_id="mcp-9-1", source="mcp",
                                                    git_commit="c" * 40, cwd="."))
        m = I.ActorMap()
        self.assertEqual(m.actor_of(rec), "claude:?")
        proxy = I.ControlEvent(id="e0", ts="t0", kind="provenance_link", target="o-1",
                               provenance=I.Provenance(host="codex", session_id="x", source="cli"))
        self.assertFalse(m.add_event(proxy), "a proxy cannot vouch for a proxy")
        self.assertFalse(m.add_event(I.ControlEvent(id="e9", ts="t", kind="seen", target="o-1",
                                                    provenance=_prov())))
        link = I.ControlEvent(id="e1", ts="t1", kind="provenance_link", target="o-1",
                              provenance=I.Provenance(host="claude", session_id="S", subagent_id="A",
                                                      subagent_type="explorer", source="hook:PostToolUse"))
        self.assertTrue(m.add_event(link))
        later = I.ControlEvent(id="e2", ts="t2", kind="provenance_link", target="o-1",
                               provenance=I.Provenance(host="claude", session_id="S", subagent_id="B",
                                                       source="hook:PostToolUse"))
        self.assertFalse(m.add_event(later), "first link wins")
        self.assertTrue(m.linked("o-1"))
        self.assertEqual(m.actor_of(rec), "claude:S:A")
        eff = m.provenance_of(rec)
        self.assertEqual((eff.git_commit, eff.cwd, eff.subagent_type), ("c" * 40, ".", "explorer"))
        self.assertEqual(I.actor_key(rec.provenance), "claude:?", "the raw record is never mutated")

    def test_b1_status_rule(self):
        base = dict(ts_a="t0", ts_b="t1", commit_a="a" * 40, commit_b="a" * 40, worktree_known=True)
        same = I.ScopeFacts(**base)
        self.assertTrue(same.unchanged)
        self.assertEqual(I.b1_status_rule("fail", "fail", same), "supports")
        self.assertEqual(I.b1_status_rule("fail", "pass", same), "refutes")
        changed = [I.ScopeFacts(**dict(base, edited_paths_between=["src/x.py"])),
                   I.ScopeFacts(**dict(base, dirty_changed_paths=["src/x.py"])),
                   I.ScopeFacts(**dict(base, commit_b="b" * 40)),
                   I.ScopeFacts(**dict(base, commit_a=None, commit_b=None)),
                   I.ScopeFacts(**dict(base, worktree_known=False))]
        for f in changed:
            self.assertFalse(f.unchanged)
            self.assertEqual(I.b1_status_rule("fail", "pass", f), "outdated", "a fix is not a refutation")
            self.assertIsNone(I.b1_status_rule("pass", "pass", f))
        self.assertFalse(I.ScopeFacts(ts_a="a", ts_b="b", commit_a="x", commit_b="x").unchanged,
                         "worktree state unknown by default")
        with self.assertRaises(ValueError):
            I.b1_status_rule("ok", "pass", same)
        _roundtrip(self, changed[0])

    def test_outdated_status_is_not_a_refutation(self):
        self.assertIn("outdated", I.CLAIM_STATUSES)
        self.assertNotIn("outdated", I.TEMPLATE_LABELS["B1"])
        for lang in I.LANGS:
            self.assertIn("outdated", I.STATUS_TAGS[lang])

    def test_model_visible_time_is_absolute(self):
        self.assertEqual(I.jev_time("2026-09-24T13:05:59.123456Z"), "2026-09-24T13:05Z")
        self.assertIsNone(I.jev_time(None))
        rel = re.compile(I.RELATIVE_TIME_RE)
        for s in ("codex session, 2h ago", "5m ago", "3 minutes ago", "3 小时前", "just now"):
            self.assertTrue(rel.search(s), s)
        self.assertIsNone(rel.search("command run by codex at %s, commit a1b2c3d"
                                     % I.jev_time("2026-09-24T13:05:59.1Z")))

    def test_secret_patterns(self):
        assign = re.compile(I.SECRET_ASSIGNMENT_RE)
        hits = (("export STRIPE_KEY=sk_live_abcdef123", "sk_live_abcdef123"), ("OPENAI_KEY=abcd1234efgh", "abcd1234efgh"),
                ("DB_PASS=hunter22", "hunter22"), ('{"api_key": "zzzz9999"}', "zzzz9999"),
                ("GITHUB_TOKEN=ghp_x1y2z3w4", "ghp_x1y2z3w4"), ("password = 'hunter2'", "hunter2"),
                ("AWS_SECRET_ACCESS_KEY: abcdEFGH1234", "abcdEFGH1234"))
        for s, v in hits:
            m = assign.search(s)
            self.assertTrue(m, s)
            self.assertEqual(m.group(3), v, s)
        for s in ("PATH=/usr/bin:/bin", "KeyError: missing_column", "Author: Alice Smith", "3 passed, 1 failed",
                  "passed: true", "HOME=/home/u"):
            self.assertIsNone(assign.search(s), s)
        name = re.compile(I.SECRET_NAME_RE)
        for n in ("STRIPE_KEY", "OPENAI_KEY", "DB_PASS", "AWS_SECRET_ACCESS_KEY", "PGPASSWORD", "TYPESAFE_API_KEY"):
            self.assertTrue(name.match(n), n)
        for n in ("PATH", "HOME", "LANG", "SHELL", "USER"):
            self.assertIsNone(name.match(n), n)
        ctx = re.compile(I.HEX_SAFE_CONTEXT_RE)
        self.assertTrue(ctx.search("commit "))
        self.assertTrue(ctx.search("sha256: "))
        self.assertIsNone(ctx.search("TOKEN="))
        self.assertTrue({"env", "printenv", "export", "set"} <= set(I.ENV_DUMP_WORDS))
        priv = I.DEFAULT_CONFIG["privacy"]
        self.assertTrue(priv["withhold_env_dumps"])
        self.assertEqual(priv["redact_hex_min_len"], I.HEX_SECRET_MIN_LEN)
        for g in (".env", ".ssh/*", "id_rsa*", "environ"):
            self.assertIn(g, priv["exclude_globs"])

    def test_jev_health_is_shared_only(self):
        fields = {f.name for f in dataclasses.fields(I.JevHealth)}
        self.assertFalse(fields & set(I.JEV_LOCAL_REASONS))
        self.assertNotIn("disabled_until", fields, "no project-wide switch-off for process-local reasons")
        fp = I.key_fingerprint("secret-value-123")
        self.assertTrue(fp.startswith("kf-") and fp == I.key_fingerprint("secret-value-123"))
        self.assertNotEqual(fp, I.key_fingerprint("other"))
        h = I.JevHealth(auth_denied={fp: "2026-09-24T01:00:00.000000Z"})
        _roundtrip(self, h)
        self.assertNotIn("secret-value-123", json.dumps(h.to_dict()))
        w = I.WorkerInfo(pid=1, started_ts="t", jev_capable=False, jev_unavailable_reason="no_key",
                         mode="single_pass", launched_by="mcp:codex")
        _roundtrip(self, w)
        self.assertIn(w.jev_unavailable_reason, I.JEV_LOCAL_REASONS)
        self.assertIn(w.mode, I.WORKER_MODES)
        self.assertIn("CODEX_SANDBOX_NETWORK_DISABLED", I.SANDBOX_NO_NETWORK_ENV)

    def test_record_echo_and_links(self):
        line = I.RECORD_ECHO_PREFIX + I.obs_id_for("cli:x")
        m = re.search(I.OBS_ID_RE, line)
        self.assertEqual(m.group(0), I.obs_id_for("cli:x"))
        self.assertIn("provenance_link", I.EVENT_KINDS)

    def test_staleness_and_shared_hook_records(self):
        _roundtrip(self, I.Brief(text="x", memory_as_of="2026-09-24T00:00:00.000000Z", stale=True))
        _roundtrip(self, I.CheckResult(decision="allow", memory_as_of="t", stale=True))
        st = I.MemoryState(obs_offset=123)
        self.assertEqual(I.MemoryState.from_dict(json.loads(json.dumps(st.to_dict()))).obs_offset, 123)
        self.assertIn("reused", I.INSTALL_ACTIONS)
        _roundtrip(self, I.InstallRecord(path=".git/hooks/pre-commit", action="reused", host="git", shared=True))

    def test_new_config_keys(self):
        c = I.DEFAULT_CONFIG
        self.assertGreater(c["extract"]["link_grace_s"], 0)
        self.assertGreater(c["worker"]["rebuild_min_interval_s"], 0)
        self.assertGreater(c["mcp"]["rebuild_budget_s"], 0)
        self.assertLessEqual(c["hooks"]["hook_rebuild_max_obs"], 5000)


if __name__ == "__main__":
    unittest.main()

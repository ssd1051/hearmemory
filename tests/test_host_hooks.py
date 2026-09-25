"""Central hook dispatcher tests: run_hook() robustness, SessionStart brief rendering
with fake memory, worker spawn, and the hook time-budget requirement (a slow Codex import must
not stop git pre-commit from producing its check text and exiting per mode)."""
import json
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_host_common import FakeStore, make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.host import _deps, git as hgit, hooks  # noqa: E402


class RunHookRobustness(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_host_is_noop(self):
        result = hooks.run_hook("notahost", "SessionStart", b"{}", root=str(self.root))
        self.assertEqual(result.exit_code, 0)

    def test_event_not_in_hosts_vocabulary_is_noop(self):
        result = hooks.run_hook("claude", "NotARealClaudeEvent", b"{}", root=str(self.root))
        self.assertEqual(result.exit_code, 0)

    def test_root_none_falls_back_to_payload_cwd(self):
        payload = json.dumps({"session_id": "s1", "cwd": str(self.root)}).encode()
        result = hooks.run_hook("claude", "SessionStart", payload, root=None)
        self.assertEqual(result.exit_code, 0)

    def test_broken_json_root_argument_never_raises(self):
        result = hooks.run_hook("claude", "SessionStart", b"{}", root="\x00bad\x00path")
        self.assertEqual(result.exit_code, 0)

    def test_adapter_exception_is_swallowed(self):
        import hearmemory.host as hostpkg

        class Boom:
            name = "claude"

            def handle_hook(self, event, payload):
                raise RuntimeError("boom")

        old = hostpkg.ADAPTERS["claude"]
        hostpkg.ADAPTERS["claude"] = Boom()
        try:
            result = hooks.run_hook("claude", "SessionStart", b"{}", root=str(self.root))
            self.assertEqual(result.exit_code, 0)
        finally:
            hostpkg.ADAPTERS["claude"] = old


class SessionStartBrief(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name))
        self.store = FakeStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_mem(self, brief_text: str):
        old = (_deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker)
        _deps.open_store = lambda root: self.store
        _deps.load_memory = lambda store, cfg, allow_rebuild=False: object()
        _deps.build_brief = lambda state, store, req, cfg: I.Brief(text=brief_text)
        spawned = []
        _deps.spawn_worker = lambda root, launched_by=None: spawned.append(launched_by) or True
        return old, spawned

    def _restore(self, old):
        _deps.open_store, _deps.load_memory, _deps.build_brief, _deps.spawn_worker = old

    def test_claude_session_start_emits_additional_context(self):
        old, spawned = self._patch_mem("[REFUTED] the cache fix; open issue: flaky test")
        try:
            payload = json.dumps({"session_id": "s1", "cwd": str(self.root)}).encode()
            result = hooks.run_hook("claude", "SessionStart", payload, root=str(self.root))
            data = json.loads(result.stdout)
            self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("REFUTED", data["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(spawned, ["hook:claude:SessionStart"])
        finally:
            self._restore(old)

    def test_cursor_session_start_shape(self):
        old, _ = self._patch_mem("hello from memory")
        try:
            payload = json.dumps({"conversation_id": "c1"}).encode()
            result = hooks.run_hook("cursor", "sessionStart", payload, root=str(self.root))
            data = json.loads(result.stdout)
            # Cursor's documented sessionStart output field is snake_case.
            self.assertEqual(data["additional_context"], "hello from memory")
            self.assertNotIn("additionalContext", data)
        finally:
            self._restore(old)

    def test_empty_brief_emits_no_stdout(self):
        old, _ = self._patch_mem("")
        try:
            payload = json.dumps({"session_id": "s1"}).encode()
            result = hooks.run_hook("claude", "SessionStart", payload, root=str(self.root))
            self.assertEqual(result.stdout, "")
        finally:
            self._restore(old)

    def test_session_start_never_crashes_without_mem(self):
        # Default state in this checkout: hearmemory.memory is not importable yet.
        payload = json.dumps({"session_id": "s1"}).encode()
        result = hooks.run_hook("claude", "SessionStart", payload, root=str(self.root))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")


class SlowImportBudget(unittest.TestCase):
    """Artificially slowing the import (sleep) must not stop git pre-commit
    from producing its check text and exiting per mode."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name), git=True)
        self.store = FakeStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_precommit_still_outputs_check_text_when_import_hangs(self):
        import hearmemory.host.codex as codex_mod

        def slow_import(store, cfg, *a, **k):
            time.sleep(2.0)
            return 0

        cfg = dict(I.DEFAULT_CONFIG)
        cfg["precommit"] = dict(cfg["precommit"], git_mode="warn")
        old = (_deps.open_store, _deps.load_memory, _deps.mem_check, _deps.load_config,
              codex_mod.import_rollouts)
        _deps.open_store = lambda root: self.store
        _deps.load_memory = lambda *a, **k: object()
        _deps.mem_check = lambda *a, **k: I.CheckResult(decision="warn", text="unresolved issue: x",
                                                        mode="warn")
        _deps.load_config = lambda root: cfg
        codex_mod.import_rollouts = slow_import
        try:
            t0 = time.monotonic()
            result = hgit.handle_hook("pre-commit", {"project": str(self.root)})
            elapsed_s = time.monotonic() - t0
            self.assertEqual(result.exit_code, 0)
            self.assertIn("unresolved issue", result.stderr)
            # precommit's import_codex slice is 350ms (DEFAULT_CONFIG hooks.timeout_ms=1500); the
            # 2s sleep must not be waited out.
            self.assertLess(elapsed_s, 1.0, f"pre-commit waited {elapsed_s:.2f}s for a slow import")
        finally:
            (_deps.open_store, _deps.load_memory, _deps.mem_check, _deps.load_config,
             codex_mod.import_rollouts) = old


if __name__ == "__main__":
    unittest.main()

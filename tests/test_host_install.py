"""Top-level install()/uninstall() orchestration tests: byte-for-byte round trip,
existing-file preservation, no writes under $HOME, purge ordering."""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_host_common import make_project  # noqa: E402

import hearmemory.interfaces as I  # noqa: E402
from hearmemory.host import install as host_install  # noqa: E402


def _snapshot(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class RoundTrip(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name), git=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_then_purge_restores_pre_init_state_byte_for_byte(self):
        # snapshot everything EXCEPT .hearmemory itself (hearmemory init creates .hearmemory/VERSION before
        # install() runs; this test only exercises the host layer's install()/uninstall()).
        before = {k: v for k, v in _snapshot(self.root).items() if not k.startswith(".hearmemory")}
        host_install.install(self.root, ["claude", "codex", "git"], dict(I.DEFAULT_CONFIG),
                             python="/usr/bin/python3")
        notes = host_install.uninstall(self.root, purge=True)
        self.assertFalse((self.root / I.HEARMEMORY_DIRNAME).exists(), notes)
        after = {k: v for k, v in _snapshot(self.root).items() if not k.startswith(".hearmemory")}
        self.assertEqual(before, after)

    def test_reinstall_is_idempotent(self):
        m1 = host_install.install(self.root, ["claude", "codex", "git"], dict(I.DEFAULT_CONFIG),
                                  python="/usr/bin/python3")
        m2 = host_install.install(self.root, ["claude", "codex", "git"], dict(I.DEFAULT_CONFIG),
                                  python="/usr/bin/python3")
        # same set of paths, no duplicated markers/keys
        text = (self.root / "AGENTS.md").read_text()
        self.assertEqual(text.count("hearmemory:begin"), 1)
        self.assertEqual({r.path for r in m1.records}, {r.path for r in m2.records})

    def test_uninstall_without_purge_keeps_hearmemory_dir(self):
        host_install.install(self.root, ["claude"], dict(I.DEFAULT_CONFIG), python="/usr/bin/python3")
        host_install.uninstall(self.root, purge=False)
        self.assertTrue((self.root / I.HEARMEMORY_DIRNAME / "VERSION").exists())
        self.assertFalse((self.root / I.HEARMEMORY_DIRNAME / "host" / "claude" / "mcp.json").exists())

    def test_uninstall_preserves_user_edited_generated_file(self):
        host_install.install(self.root, ["claude"], dict(I.DEFAULT_CONFIG), python="/usr/bin/python3")
        launch = self.root / I.HEARMEMORY_DIRNAME / "host" / "claude" / "launch.sh"
        launch.write_text(launch.read_text() + "\necho user-added-line\n", encoding="utf-8")
        host_install.uninstall(self.root, purge=False)
        self.assertFalse(launch.exists())  # moved to archive, not left in place
        archived = list((self.root / I.HEARMEMORY_DIRNAME / "archive").rglob("launch.sh"))
        self.assertEqual(len(archived), 1)
        self.assertIn("user-added-line", archived[0].read_text())


class NoUserLevelWrites(unittest.TestCase):
    """Temp HOME stays empty across install+several hooks+uninstall."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.fake_home = Path(self._tmp.name) / "home"
        self.fake_home.mkdir()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.fake_home)
        self.proj_tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self.proj_tmp.name), git=True)

    def tearDown(self):
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        else:
            os.environ.pop("HOME", None)
        self._tmp.cleanup()
        self.proj_tmp.cleanup()

    def test_home_untouched_by_install_hooks_and_uninstall(self):
        from hearmemory.host import hooks
        host_install.install(self.root, ["claude", "codex", "cursor", "git"], dict(I.DEFAULT_CONFIG),
                             python="/usr/bin/python3")
        payload = json.dumps({"session_id": "s1", "cwd": str(self.root), "tool_name": "Bash",
                              "tool_input": {"command": "echo hi"}, "tool_response": {"exit_code": 0},
                              "tool_use_id": "t1"}).encode()
        hooks.run_hook("claude", "PostToolUse", payload, root=str(self.root))
        hooks.run_hook("claude", "SessionStart", b'{"session_id": "s1"}', root=str(self.root))
        host_install.uninstall(self.root, purge=True)
        self.assertEqual(list(self.fake_home.iterdir()), [])


class Force(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_project(Path(self._tmp.name), git=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_force_true_skips_carry_forward_but_still_writes_cleanly(self):
        host_install.install(self.root, ["claude"], dict(I.DEFAULT_CONFIG), python="/usr/bin/python3")
        manifest = host_install.install(self.root, ["claude"], dict(I.DEFAULT_CONFIG),
                                        python="/usr/bin/python3", force=True)
        self.assertTrue(any(r.path.endswith("mcp.json") for r in manifest.records))


if __name__ == "__main__":
    unittest.main()

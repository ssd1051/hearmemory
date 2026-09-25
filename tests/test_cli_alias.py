"""The short alias `hmem` is the same command as `hearmemory`: installed as a console script, and recognised
wherever hearmemory detects its own commands (self-command skip, record linking)."""
import unittest
from importlib import metadata

from hearmemory.host import claude as claude_host
from hearmemory.host import codex as codex_host
from hearmemory.textutil import hearmemory_only_command, strip_hearmemory_output


class AliasTests(unittest.TestCase):
    def test_both_console_scripts_point_to_the_same_main(self):
        eps = {ep.name: ep.value for ep in metadata.entry_points(group="console_scripts")
               if ep.name in ("hearmemory", "hmem")}
        self.assertEqual(eps, {"hearmemory": "hearmemory.cli:main", "hmem": "hearmemory.cli:main"})

    def test_alias_only_commands(self):
        self.assertTrue(hearmemory_only_command('hmem record --kind claim "a && b"'))
        self.assertTrue(hearmemory_only_command("cd /x && hmem check --staged"))
        self.assertTrue(hearmemory_only_command(".venv/bin/hmem recall --brief"))
        self.assertFalse(hearmemory_only_command("hmem record x && git commit -m y"))
        self.assertFalse(hearmemory_only_command("hmemory status"))

    def test_alias_is_a_hearmemory_cli_call(self):
        for mod in (claude_host, codex_host):
            self.assertTrue(mod._is_hearmemory_cli("hmem check --staged"))
            self.assertTrue(mod._is_hearmemory_cli("pytest -q && hmem record --kind claim ok"))
            self.assertFalse(mod._is_hearmemory_cli("echo hmemory"))

    def test_alias_record_is_linked(self):
        out = "hearmemory: recorded o-0123456789abcdef\n[main 1a2b3c4] x\n"
        self.assertEqual(codex_host._cli_record_ids("hmem record --kind claim ok && git commit -m x", out),
                         ["o-0123456789abcdef"])
        self.assertEqual(strip_hearmemory_output(out), "[main 1a2b3c4] x\n")


if __name__ == "__main__":
    unittest.main()

"""config.py -- deep merge, type-mismatch fallback, render/load roundtrip."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hearmemory.config import deep_merge, load_config, load_config_report, render_default_config, write_default_config
from hearmemory.interfaces import DEFAULT_CONFIG


class TestConfig(unittest.TestCase):
    def test_deep_merge_overrides_known_keys(self):
        merged, unknown, mismatched = deep_merge(DEFAULT_CONFIG, {"jev": {"enabled": False}})
        self.assertFalse(merged["jev"]["enabled"])
        self.assertEqual(merged["jev"]["model"], DEFAULT_CONFIG["jev"]["model"])
        self.assertEqual(unknown, [])
        self.assertEqual(mismatched, [])

    def test_deep_merge_keeps_unknown_keys(self):
        merged, unknown, _ = deep_merge(DEFAULT_CONFIG, {"experimental": {"foo": 1}})
        self.assertEqual(merged["experimental"], {"foo": 1})
        self.assertIn("experimental", unknown)

    def test_deep_merge_type_mismatch_falls_back_to_default(self):
        merged, _, mismatched = deep_merge(DEFAULT_CONFIG, {"jev": {"daily_call_cap": "not-a-number"}})
        self.assertEqual(merged["jev"]["daily_call_cap"], DEFAULT_CONFIG["jev"]["daily_call_cap"])
        self.assertIn("jev.daily_call_cap", mismatched)

    def test_render_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".hearmemory").mkdir()
            write_default_config(root)
            cfg = load_config(root)
            for section, values in DEFAULT_CONFIG.items():
                for key, value in values.items():
                    if key == "name" and section == "project":
                        continue
                    self.assertEqual(cfg[section][key], value, f"{section}.{key}")

    def test_load_config_without_file_uses_defaults_and_project_name(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "myproj"
            root.mkdir()
            cfg = load_config(root)
            self.assertEqual(cfg["project"]["name"], "myproj")
            self.assertEqual(cfg["hosts"]["enabled"], list(DEFAULT_CONFIG["hosts"]["enabled"]))

    def test_load_config_report_surfaces_unknown_and_mismatched(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".hearmemory").mkdir()
            (root / ".hearmemory" / "config.toml").write_text(
                '[jev]\nenabled = "yes"\n[weird]\nx = 1\n', encoding="utf-8")
            cfg, unknown, mismatched = load_config_report(root)
            self.assertTrue(cfg["jev"]["enabled"])  # falls back to default True
            self.assertIn("jev.enabled", mismatched)
            self.assertIn("weird", unknown)

    def test_render_default_config_only_uses_supported_scalar_types(self):
        text = render_default_config()
        self.assertIn("[jev]", text)
        self.assertIn("daily_call_cap = 200", text)
        self.assertIn('model = "jev-1.13.0"', text)


if __name__ == "__main__":
    unittest.main()

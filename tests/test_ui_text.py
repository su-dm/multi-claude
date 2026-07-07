"""Tests for the curses-free parts of the sidebar UI: text wrapping (nothing
may be cut off), the popup help text, tmux version parsing, and the
persisted sidebar width setting."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from multi_claude.config import Config, SIDEBAR_WIDTH
from multi_claude.tmux import parse_tmux_version
from multi_claude.ui import HELP_LINES, _wrap, help_text


class WrapTest(unittest.TestCase):
    def test_wraps_without_losing_content(self):
        text = "existing-plain already works here (no git → no worktree) — share dir? y/N"
        lines = _wrap(text, 32)
        self.assertTrue(all(len(line) <= 32 for line in lines))
        self.assertEqual(" ".join(lines), text)

    def test_breaks_words_longer_than_width(self):
        lines = _wrap("a" * 50, 20)
        self.assertTrue(all(len(line) <= 20 for line in lines))
        self.assertEqual("".join(lines), "a" * 50)

    def test_empty_text_yields_no_lines(self):
        self.assertEqual(_wrap("", 20), [])

    def test_degenerate_width_still_returns_everything(self):
        lines = _wrap("abc", 0)
        self.assertEqual("".join(lines), "abc")


class HelpTextTest(unittest.TestCase):
    def test_mentions_every_help_entry(self):
        text = help_text()
        for key, desc in HELP_LINES:
            if key:
                self.assertIn(key, text)
            self.assertIn(desc, text)

    def test_documents_new_keys(self):
        text = help_text()
        self.assertIn("< / >", text)          # sidebar width
        self.assertIn("C-c", text)            # graceful quit
        self.assertIn("quit dashboard", text)


class TmuxVersionParseTest(unittest.TestCase):
    def test_release_formats(self):
        self.assertEqual(parse_tmux_version("tmux 3.4"), (3, 4))
        self.assertEqual(parse_tmux_version("tmux 3.3a"), (3, 3))
        self.assertEqual(parse_tmux_version("tmux next-3.6"), (3, 6))

    def test_garbage_is_unsupported(self):
        self.assertEqual(parse_tmux_version(""), (0, 0))
        self.assertEqual(parse_tmux_version("tmux master"), (0, 0))


class SidebarWidthSettingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def make_config(self) -> Config:
        return Config(socket_name="mc-test-none", data_dir=Path(self.dir.name))

    def test_default_width(self):
        self.assertEqual(self.make_config().sidebar_width, SIDEBAR_WIDTH)

    def test_saved_width_roundtrips(self):
        cfg = self.make_config()
        cfg.save_setting("sidebar_width", 48)
        fresh = self.make_config()
        fresh.apply_saved_settings()
        self.assertEqual(fresh.sidebar_width, 48)

    def test_saved_width_is_clamped_and_bad_values_ignored(self):
        cfg = self.make_config()
        cfg.save_setting("sidebar_width", 500)
        fresh = self.make_config()
        fresh.apply_saved_settings()
        self.assertEqual(fresh.sidebar_width, 100)
        cfg.save_setting("sidebar_width", "junk")
        fresh2 = self.make_config()
        fresh2.apply_saved_settings()
        self.assertEqual(fresh2.sidebar_width, SIDEBAR_WIDTH)

    def test_env_var_beats_saved_setting(self):
        cfg = self.make_config()
        cfg.save_setting("sidebar_width", 48)
        with mock.patch.dict(os.environ, {"MULTI_CLAUDE_SIDEBAR_WIDTH": "40"}):
            fresh = self.make_config()
            fresh.apply_saved_settings()
            self.assertEqual(fresh.sidebar_width, 40)

    def test_settings_file_keeps_other_keys(self):
        cfg = self.make_config()
        cfg.save_setting("notify", False)
        cfg.save_setting("sidebar_width", 40)
        data = json.loads(cfg.settings_path.read_text())
        self.assertEqual(data, {"notify": False, "sidebar_width": 40})


if __name__ == "__main__":
    unittest.main()

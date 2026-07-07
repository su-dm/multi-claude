"""Tests for the tmux wrapper that don't need a running server: argv
construction, config generation, and env handling for nested attach."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from multi_claude.config import Config, DASH_SESSION, WORK_SESSION
from multi_claude.tmux import Tmux


def make_config(tmpdir: str) -> Config:
    return Config(socket_name="mc-test-none", data_dir=Path(tmpdir))


class TmuxArgvTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmux = Tmux(make_config(self.dir.name))

    def tearDown(self):
        self.dir.cleanup()

    def test_base_argv_targets_dedicated_socket_and_conf(self):
        argv = self.tmux.base_argv()
        self.assertEqual(argv[:3], ["tmux", "-L", "mc-test-none"])
        self.assertEqual(argv[3], "-f")
        self.assertTrue(argv[4].endswith("tmux.conf"))

    def test_attach_argv_targets_dashboard(self):
        argv = self.tmux.attach_dashboard_argv()
        self.assertEqual(argv[-3:], ["attach-session", "-t", f"={DASH_SESSION}"])

    def test_attach_env_drops_tmux_var(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,123,0"}):
            self.assertNotIn("TMUX", self.tmux.attach_env())

    def test_write_conf_includes_bindings_and_hooks(self):
        self.tmux.write_conf()
        conf = self.tmux.config.tmux_conf_path.read_text()
        self.assertIn("remain-on-exit on", conf)
        self.assertIn("detach-client", conf)
        self.assertIn("M-h select-pane -L", conf)
        self.assertIn("select 1", conf)
        self.assertIn("select next", conf)
        self.assertIn("MULTI_CLAUDE_SOCKET=mc-test-none", conf)
        # C-c on a dead pane quits the dashboard; live panes get C-c as-is.
        self.assertIn('if-shell -F "#{pane_dead}"', conf)
        self.assertIn("send-keys C-c", conf)

    def test_write_conf_macos_option_key_fallbacks(self):
        self.tmux.write_conf()
        conf = self.tmux.config.tmux_conf_path.read_text()
        # ˙/¬ are what Option+h/l type on a macOS US layout.
        if sys.platform == "darwin":
            self.assertIn("bind-key -n ˙ select-pane -L", conf)
            self.assertIn("bind-key -n ¬ select-pane -R", conf)
        else:
            self.assertNotIn("˙", conf)

    def test_send_text_keeps_leading_dash_literal(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.tmux.send_text("%1", "-N hello")
        argvs = [c.args[0] for c in run.call_args_list]
        literal = next(a for a in argvs if "-l" in a)
        self.assertEqual(literal[-2:], ["--", "-N hello"])

    def test_spawn_instance_argv(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="%7\n", stderr="")
            pane_id = self.tmux.spawn_instance("proj", "/tmp", ["/usr/bin/claude", "--continue"])
        self.assertEqual(pane_id, "%7")
        argvs = [c.args[0] for c in run.call_args_list]
        spawn = next(a for a in argvs if "new-window" in a)
        self.assertEqual(spawn[spawn.index("-n") + 1], "proj")
        self.assertEqual(spawn[spawn.index("-c") + 1], "/tmp")
        self.assertEqual(spawn[spawn.index("-t") + 1], f"={WORK_SESSION}:")
        self.assertEqual(spawn[-1], "/usr/bin/claude --continue")

    def test_queries_return_neutral_when_no_server(self):
        # Socket "mc-test-none" has no server; these must not raise.
        self.assertEqual(self.tmux.list_sessions(), [])
        self.assertEqual(self.tmux.list_panes(), [])
        self.assertFalse(self.tmux.pane_exists("%1"))
        self.assertEqual(self.tmux.capture_pane("%1"), "")
        self.assertFalse(self.tmux.dashboard_exists())


if __name__ == "__main__":
    unittest.main()

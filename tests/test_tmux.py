"""Tests for the tmux wrapper that don't need a running server: argv
construction, config generation, and env handling for nested attach."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from multi_claude.config import Config
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

    def test_attach_argv_uses_exact_session_match(self):
        argv = self.tmux.attach_argv("proj")
        self.assertEqual(argv[-3:], ["attach-session", "-t", "=proj"])

    def test_attach_env_drops_tmux_var(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,123,0"}):
            self.assertNotIn("TMUX", self.tmux.attach_env())

    def test_write_conf_creates_dedicated_config(self):
        self.tmux.write_conf()
        conf = self.tmux.config.tmux_conf_path.read_text()
        self.assertIn("remain-on-exit on", conf)
        self.assertIn("detach-client", conf)

    def test_new_session_argv(self):
        calls = []
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.tmux.new_session("proj", "/tmp", ["/usr/bin/claude", "--continue"])
            calls = [c.args[0] for c in run.call_args_list]
        (argv,) = calls
        self.assertIn("new-session", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "proj")
        self.assertEqual(argv[argv.index("-c") + 1], "/tmp")
        self.assertEqual(argv[-1], "/usr/bin/claude --continue")

    def test_queries_return_neutral_when_no_server(self):
        # Socket "mc-test-none" has no server; these must not raise.
        self.assertEqual(self.tmux.list_sessions(), [])
        self.assertFalse(self.tmux.server_running())
        self.assertIsNone(self.tmux.pane_info("nope"))
        self.assertEqual(self.tmux.capture_pane("nope"), "")


if __name__ == "__main__":
    unittest.main()

"""Statusline cost-capture tests."""

import json
import tempfile
import unittest
from pathlib import Path

from multi_claude.config import Config
from multi_claude.statusline import (
    chain_path,
    install,
    record_and_render,
    reported_cost,
    uninstall,
)


def payload(session_id="s1", cost=1.234, model="Sonnet 5"):
    return json.dumps({
        "session_id": session_id,
        "model": {"id": "claude-sonnet-5", "display_name": model},
        "cost": {"total_cost_usd": cost},
        "workspace": {"current_dir": "/tmp"},
    })


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.tmp.name), socket_name="mc-test-none")

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_cost_and_renders(self):
        out = record_and_render(self.config, payload())
        self.assertIn("Sonnet 5", out)
        self.assertIn("$1.23", out)
        self.assertEqual(reported_cost(self.config, "s1"), 1.234)

    def test_updates_overwrite(self):
        record_and_render(self.config, payload(cost=1.0))
        record_and_render(self.config, payload(cost=2.5))
        self.assertEqual(reported_cost(self.config, "s1"), 2.5)

    def test_missing_session_reports_none(self):
        self.assertIsNone(reported_cost(self.config, "nope"))
        self.assertIsNone(reported_cost(self.config, ""))

    def test_traversal_session_id_writes_nothing(self):
        # The session id becomes a filename; a crafted payload must not be
        # able to escape costs_dir (or write there under a weird name).
        for evil in ("../../escaped", "a/b", ".", "x" * 65, "a\nb"):
            out = record_and_render(self.config, payload(session_id=evil))
            self.assertIn("Sonnet 5", out)  # still renders a statusline
        written = [
            p for p in Path(self.tmp.name).rglob("*")
            if p.is_file() and p.suffix in (".json", ".tmp")
        ]
        self.assertEqual(written, [])
        self.assertEqual(list(Path(self.tmp.name).parent.glob("escaped.json")), [])

    def test_bad_payload_does_not_crash(self):
        out = record_and_render(self.config, "{not json")
        self.assertIn("bad statusline payload", out)

    def test_chains_previous_statusline(self):
        self.config.ensure_dirs()
        chain_path(self.config).write_text("echo chained-output\n")
        out = record_and_render(self.config, payload())
        self.assertEqual(out, "chained-output")
        # cost still captured even when chaining
        self.assertEqual(reported_cost(self.config, "s1"), 1.234)


class InstallUninstallTest(unittest.TestCase):
    """install.sh wires the hook by default, so the install/revert pair must
    be idempotent and must round-trip a pre-existing statusline exactly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.config = Config(
            data_dir=base / "data",
            claude_home=base / "claude-home",
            socket_name="mc-test-none",
        )
        self.settings = self.config.claude_home / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def read_settings(self) -> dict:
        return json.loads(self.settings.read_text())

    def test_install_then_uninstall_with_no_prior_statusline(self):
        install(self.config)
        cmd = self.read_settings()["statusLine"]["command"]
        self.assertIn("multi_claude statusline", cmd)
        uninstall(self.config)
        self.assertNotIn("statusLine", self.read_settings())

    def test_prior_statusline_is_chained_and_restored(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "my-own-line"},
             "other": "kept"}
        ))
        install(self.config)
        self.assertEqual(chain_path(self.config).read_text().strip(), "my-own-line")
        self.assertEqual(self.read_settings()["other"], "kept")
        uninstall(self.config)
        after = self.read_settings()
        self.assertEqual(after["statusLine"]["command"], "my-own-line")
        self.assertEqual(after["other"], "kept")
        self.assertFalse(chain_path(self.config).exists())

    def test_reinstall_does_not_chain_our_own_hook(self):
        install(self.config)
        install(self.config)  # e.g. install.sh run twice
        self.assertFalse(chain_path(self.config).exists())

    def test_uninstall_when_not_installed_is_a_noop(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "my-own-line"}}
        ))
        note = uninstall(self.config)
        self.assertIn("nothing to do", note)
        self.assertEqual(self.read_settings()["statusLine"]["command"], "my-own-line")


if __name__ == "__main__":
    unittest.main()

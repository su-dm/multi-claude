"""Statusline cost-capture tests."""

import json
import tempfile
import unittest
from pathlib import Path

from multi_claude.config import Config
from multi_claude.statusline import chain_path, record_and_render, reported_cost


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


if __name__ == "__main__":
    unittest.main()

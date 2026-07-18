"""Manager-level logic that doesn't need a live tmux server: stale pane-id
reconciliation and notification debouncing.

Background (the bug these encode): tmux pane ids are only unique within one
server lifetime. After a reboot, a registry entry from the previous server
can point at a pane id that now belongs to a different instance — the stale
entry then mirrors that pane's screen and fires phantom notifications for an
agent nobody touched.
"""

import tempfile
import unittest
from pathlib import Path

from multi_claude.config import Config
from multi_claude.manager import InstanceManager, resolve_pane_claims
from multi_claude.registry import Instance
from multi_claude.status import Status
from multi_claude.tmux import Pane


def work_pane(pane_id, window_name, path, cmd="/usr/bin/claude"):
    return Pane(pane_id, "mc-work", "@9", window_name, False, cmd, path)


class ResolvePaneClaimsTest(unittest.TestCase):
    def test_recycled_pane_id_goes_to_matching_instance(self):
        stale = Instance(name="old", cwd="/tmp/old", pane_id="%3", started_at=100.0)
        owner = Instance(name="new", cwd="/tmp", pane_id="%3", started_at=200.0)
        panes = {"%3": work_pane("%3", "new", "/tmp")}
        self.assertTrue(resolve_pane_claims([stale, owner], panes))
        self.assertEqual(stale.pane_id, "")
        self.assertEqual(owner.pane_id, "%3")

    def test_cwd_match_beats_recency(self):
        # The younger claimant is NOT in the pane's cwd; the older one is.
        right = Instance(name="right", cwd="/tmp", pane_id="%3", started_at=100.0)
        wrong = Instance(name="wrong", cwd="/tmp/other", pane_id="%3", started_at=200.0)
        panes = {"%3": work_pane("%3", "right", "/tmp")}
        resolve_pane_claims([right, wrong], panes)
        self.assertEqual(right.pane_id, "%3")
        self.assertEqual(wrong.pane_id, "")

    def test_same_cwd_falls_back_to_window_name_then_recency(self):
        a = Instance(name="a", cwd="/tmp", pane_id="%3", started_at=100.0)
        b = Instance(name="b", cwd="/tmp", pane_id="%3", started_at=200.0)
        resolve_pane_claims([a, b], {"%3": work_pane("%3", "a", "/tmp")})
        self.assertEqual((a.pane_id, b.pane_id), ("%3", ""))
        # No window-name match either: most recently started wins.
        a.pane_id = b.pane_id = "%3"
        resolve_pane_claims([a, b], {"%3": work_pane("%3", "zzz", "/tmp")})
        self.assertEqual((a.pane_id, b.pane_id), ("", "%3"))

    def test_claim_on_furniture_pane_is_cleared(self):
        stale = Instance(name="old", cwd="/tmp", pane_id="%2")
        keep = Pane("%2", "mc-work", "@1", "-keep", False, '"sleep 2147483647"', "/")
        self.assertTrue(resolve_pane_claims([stale], {"%2": keep}))
        self.assertEqual(stale.pane_id, "")

    def test_single_honest_claim_untouched(self):
        inst = Instance(name="me", cwd="/tmp", pane_id="%3")
        self.assertFalse(resolve_pane_claims([inst], {"%3": work_pane("%3", "me", "/tmp")}))
        self.assertEqual(inst.pane_id, "%3")

    def test_gone_pane_untouched(self):
        # Pane no longer exists: leave the claim; the snapshot shows EXITED
        # and the entry stays restartable.
        inst = Instance(name="me", cwd="/tmp", pane_id="%3")
        self.assertFalse(resolve_pane_claims([inst], {}))
        self.assertEqual(inst.pane_id, "%3")


class FakeTmux:
    """Just enough of Tmux for poll_once: one instance pane whose screen we
    script per poll."""

    def __init__(self, screens):
        self.screens = list(screens)
        self.pane = work_pane("%1", "agent", "/tmp")

    def list_panes(self):
        return [self.pane]

    def capture_pane(self, pane_id, lines=0):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def dash_panes(self):
        return []

    def dash_attached(self):
        return False


SPINNER = "✻ Hashing… (46s · ↓ 2.2k tokens)\n"
PROMPT = "──────────────\n❯ \n──────────────\n  -- INSERT -- ⏵⏵ auto mode on (shift+tab to cycle)\n"


class AttentionDebounceTest(unittest.TestCase):
    def _manager(self, screens):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Config(
            socket_name="mc-test-none",
            data_dir=Path(self.tmp.name),
            claude_home=Path(self.tmp.name) / "nohome",
        )
        mgr = InstanceManager(config)
        mgr.tmux = FakeTmux(screens)  # type: ignore[assignment]
        mgr.registry.add(Instance(name="agent", cwd="/tmp", pane_id="%1"))
        return mgr

    def test_real_work_finishing_pings(self):
        mgr = self._manager([PROMPT, SPINNER + PROMPT, SPINNER + PROMPT, PROMPT])
        for _ in range(4):
            mgr.poll_once()
        self.assertEqual(mgr.drain_attention_events(), [("agent", Status.IDLE)])

    def test_one_poll_working_blip_does_not_ping(self):
        # A single changed frame (redraw blip / stray refresh) must not
        # produce a "finished work" notification.
        mgr = self._manager([PROMPT, "redrawn stray frame", PROMPT, PROMPT])
        for _ in range(4):
            mgr.poll_once()
        self.assertEqual(mgr.drain_attention_events(), [])

    def test_help_pings_immediately_even_after_short_working(self):
        help_frame = "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
        mgr = self._manager([PROMPT, SPINNER + PROMPT, help_frame, help_frame])
        for _ in range(4):
            mgr.poll_once()
        self.assertEqual(mgr.drain_attention_events(), [("agent", Status.HELP)])


if __name__ == "__main__":
    unittest.main()

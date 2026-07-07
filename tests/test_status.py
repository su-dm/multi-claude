"""Status heuristics tests using realistic Claude Code pane frames.

If Claude Code's UI strings change and these heuristics need updating, these
fixtures document exactly what each state looked like when the markers were
chosen (Claude Code 2.1.x).
"""

import unittest

from multi_claude.status import Status, classify

BUSY_FRAME = """\
● I'll start by reading the config module.

✻ Cogitating… (esc to interrupt · 42s · ↓ 1.2k tokens)
"""

APPROVAL_FRAME = """\
╭──────────────────────────────────────────────╮
│ Bash command                                 │
│                                              │
│   rm -rf build                               │
│   Remove build artifacts                     │
│                                              │
│ Do you want to proceed?                      │
│ ❯ 1. Yes                                     │
│   2. Yes, and don't ask again this session   │
│   3. No, and tell Claude what to do          │
╰──────────────────────────────────────────────╯
"""

TRUST_FRAME = """\
 Do you trust the files in this folder?

 /home/user/code/project

 ❯ 1. Yes, proceed
   2. No, exit
"""

READY_FRAME = """\
● Done! The refactor is complete and all 34 tests pass.

╭──────────────────────────────────────────────╮
│ >                                            │
╰──────────────────────────────────────────────╯
  ? for shortcuts
"""

PLAIN_SHELL_FRAME = """\
$ ls
Makefile  README.md
$
"""


class ClassifyTest(unittest.TestCase):
    def test_dead_pane_wins(self):
        self.assertIs(classify(READY_FRAME, pane_dead=True).status, Status.EXITED)

    def test_empty_is_starting(self):
        self.assertIs(classify("").status, Status.STARTING)
        self.assertIs(classify("\n\n  \n").status, Status.STARTING)

    def test_busy(self):
        info = classify(BUSY_FRAME)
        self.assertIs(info.status, Status.BUSY)
        self.assertIn("Cogitating", info.detail)

    def test_busy_beats_prompt_box(self):
        # While working, the input box may still be on screen; busy wins.
        info = classify(READY_FRAME + "\n✻ Working… (esc to interrupt)")
        self.assertIs(info.status, Status.BUSY)

    def test_approval_dialog(self):
        info = classify(APPROVAL_FRAME)
        self.assertIs(info.status, Status.APPROVAL)
        self.assertIn("Do you want to proceed?", info.detail)

    def test_trust_prompt_is_approval(self):
        self.assertIs(classify(TRUST_FRAME).status, Status.APPROVAL)

    def test_ready_prompt(self):
        self.assertIs(classify(READY_FRAME).status, Status.READY)

    def test_unrecognized_is_unknown(self):
        self.assertIs(classify(PLAIN_SHELL_FRAME).status, Status.UNKNOWN)

    def test_old_spinner_in_scrollback_does_not_mark_busy(self):
        # Marker appears far above the tail (stale frame); tail is a prompt.
        stale = "✻ Working… (esc to interrupt)\n" + ("\n. filler" * 30) + "\n" + READY_FRAME
        self.assertIs(classify(stale).status, Status.READY)

    def test_attention_flags(self):
        self.assertTrue(Status.READY.wants_attention)
        self.assertTrue(Status.APPROVAL.wants_attention)
        self.assertTrue(Status.EXITED.wants_attention)
        self.assertFalse(Status.BUSY.wants_attention)
        self.assertFalse(Status.STARTING.wants_attention)


if __name__ == "__main__":
    unittest.main()

"""Status heuristics tests using realistic Claude Code pane frames.

If Claude Code's UI strings change and these heuristics need updating, these
fixtures document exactly what each state looked like when the markers were
chosen (Claude Code 2.1.x).
"""

import unittest

from multi_claude.status import Status, classify

WORKING_FRAME = """\
● I'll start by reading the config module.

✻ Cogitating… (esc to interrupt · 42s · ↓ 1.2k tokens)
"""

PERMISSION_FRAME = """\
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
 Quick safety check: Is this a project you created or one you trust?

 /home/user/code/project

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

IDLE_FRAME = """\
● Done! The refactor is complete and all 34 tests pass.

╭──────────────────────────────────────────────╮
│ >                                            │
╰──────────────────────────────────────────────╯
  ? for shortcuts
"""

UNRECOGNIZED_FRAME = """\
Some completely redesigned future UI
with no strings we know about.
"""

# The transcript prefixes past USER messages with the same "❯" glyph dialogs
# use for their cursor — a resting session showing history must be idle.
TRANSCRIPT_FRAME = """\
❯ /model
  ⎿  Set model to Sonnet 5 and saved as your default for new sessions
❯ hey
● Hey! What are you working on?

╭──────────────────────────────────────────────╮
│ >                                            │
╰──────────────────────────────────────────────╯
  ? for shortcuts
"""


class ClassifyTest(unittest.TestCase):
    def test_dead_pane_wins(self):
        self.assertIs(classify(IDLE_FRAME, pane_dead=True).status, Status.EXITED)

    def test_empty_is_starting(self):
        self.assertIs(classify("").status, Status.STARTING)
        self.assertIs(classify("\n\n  \n").status, Status.STARTING)

    def test_working_spinner(self):
        info = classify(WORKING_FRAME)
        self.assertIs(info.status, Status.WORKING)
        self.assertIn("Cogitating", info.detail)

    def test_working_beats_prompt_box(self):
        # While working, the input box may still be on screen; working wins.
        info = classify(IDLE_FRAME + "\n✻ Working… (esc to interrupt)")
        self.assertIs(info.status, Status.WORKING)

    def test_permission_dialog_is_help(self):
        info = classify(PERMISSION_FRAME)
        self.assertIs(info.status, Status.HELP)
        self.assertIn("Do you want to proceed?", info.detail)

    def test_trust_prompt_is_help(self):
        self.assertIs(classify(TRUST_FRAME).status, Status.HELP)

    def test_help_even_when_changed(self):
        # A dialog that just appeared (screen changed) is still HELP.
        self.assertIs(classify(PERMISSION_FRAME, changed=True).status, Status.HELP)

    def test_idle_prompt(self):
        self.assertIs(classify(IDLE_FRAME).status, Status.IDLE)

    def test_transcript_user_prompt_glyph_is_not_help(self):
        self.assertIs(classify(TRANSCRIPT_FRAME).status, Status.IDLE)

    def test_change_fallback_unrecognized_screen(self):
        # Unknown UI: a changing screen means work, a static one means idle.
        self.assertIs(classify(UNRECOGNIZED_FRAME, changed=True).status, Status.WORKING)
        self.assertIs(classify(UNRECOGNIZED_FRAME, changed=False).status, Status.IDLE)

    def test_typing_at_idle_box_is_not_working(self):
        # The idle input box with changed=True is the user typing a message
        # themselves — their keystrokes redraw the screen, but that is not
        # Claude working, and must not fire a notification when they pause.
        self.assertIs(classify(IDLE_FRAME, changed=True).status, Status.IDLE)
        typing = IDLE_FRAME.replace("│ >      ", "│ > fix t")
        self.assertIs(classify(typing, changed=True).status, Status.IDLE)

    def test_old_spinner_in_scrollback_does_not_mark_working(self):
        # Marker appears far above the tail (stale frame); tail is a prompt.
        stale = "✻ Working… (esc to interrupt)\n" + ("\n. filler" * 30) + "\n" + IDLE_FRAME
        self.assertIs(classify(stale).status, Status.IDLE)

    def test_attention_flags(self):
        self.assertTrue(Status.IDLE.wants_attention)
        self.assertTrue(Status.HELP.wants_attention)
        self.assertTrue(Status.EXITED.wants_attention)
        self.assertFalse(Status.WORKING.wants_attention)
        self.assertFalse(Status.STARTING.wants_attention)


if __name__ == "__main__":
    unittest.main()

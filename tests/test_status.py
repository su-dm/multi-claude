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

# -- current 2.1.x chrome (verified against 2.1.212, vim mode on) -----------
# The spinner dropped the "esc to interrupt" hint, and the bordered "│ >"
# input box became a bare "❯ " prompt between horizontal rules with a
# mode-dependent hint line. The input box stays visible WHILE working
# (messages can be queued), so idle can only mean "box and no spinner".

CURRENT_WORKING_FRAME = """\
● The dashboard is live. Let me capture the panes.

✻ Hashing… (4m 53s · ↓ 13.0k tokens)
  ⎿  Tip: Run /install-github-app to tag @claude right from your Github issues
────────────────────────────────────────────
❯
────────────────────────────────────────────
  Fable 5 · $2.65
  -- INSERT -- ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""

CURRENT_THINKING_FRAME = """\
* Scaffolding project… (thinking with medium effort)
────────────────────────────────────────────
❯
────────────────────────────────────────────
  -- INSERT -- ⏵⏵ auto mode on (shift+tab to cycle)
"""

CURRENT_IDLE_FRAME = """\
● Done! The refactor is complete and all tests pass.

✻ Churned for 10m 55s
────────────────────────────────────────────
❯
────────────────────────────────────────────
  Fable 5 · $15.20
  -- INSERT -- ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
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

    def test_current_working_spinner(self):
        # No "esc to interrupt" hint and the prompt box is visible while
        # working — the spinner line alone must win, even on a static poll.
        info = classify(CURRENT_WORKING_FRAME, changed=False)
        self.assertIs(info.status, Status.WORKING)
        self.assertIn("Hashing", info.detail)

    def test_current_thinking_spinner(self):
        # Extended thinking can hold the screen static for a while; the
        # spinner marker must carry it, not the change fallback.
        info = classify(CURRENT_THINKING_FRAME, changed=False)
        self.assertIs(info.status, Status.WORKING)
        self.assertIn("Scaffolding project", info.detail)

    def test_current_idle_prompt(self):
        # The finished-work summary line ("✻ Churned for 10m 55s") has no
        # "…(" and must NOT read as a spinner.
        self.assertIs(classify(CURRENT_IDLE_FRAME).status, Status.IDLE)
        # ... and typing into the new prompt redraws the screen: still idle.
        self.assertIs(classify(CURRENT_IDLE_FRAME, changed=True).status, Status.IDLE)

    def test_quoted_spinner_in_message_body_is_not_working(self):
        # Claude Code indents message/tool content two spaces; a quoted
        # spinner line (e.g. from a pasted capture) must not read as working.
        quoted = "  ✻ Hashing… (46s · ↓ 2.2k tokens)\n" + CURRENT_IDLE_FRAME
        self.assertIs(classify(quoted).status, Status.IDLE)

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

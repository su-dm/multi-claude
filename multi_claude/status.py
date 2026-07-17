"""Classify a Claude Code pane into three user-facing states:

- WORKING: Claude is busy (spinner visible, or the screen is still changing)
- HELP:    Claude is blocked on the user (permission dialog, plan approval,
           trust prompt, a question with options)
- IDLE:    Claude is done and resting at the input box

plus EXITED (process gone; pane kept via remain-on-exit) and STARTING
(nothing rendered yet).

Detection is two-layered, because marker strings are the first thing to rot
when Claude Code's UI changes:

1. Marker matching on the *visible* screen (never scrollback — it holds
   stale frames). Markers verified against Claude Code 2.1.x.
2. Screen-change fallback: the caller passes `changed` (did the visible text
   differ from the previous poll?). A changing *unrecognized* screen means
   work is happening even if we don't recognize the spinner; a static one is
   treated as idle. The fallback never fires while the resting input box is
   visible: the user typing their own message redraws the screen on every
   keystroke, and that is not Claude working. This keeps the three states
   *approximately* right even if every marker string changes.

This module is the only place that knows what Claude Code's UI looks like.
Fixtures live in tests/test_status.py.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class Status(enum.Enum):
    STARTING = "starting"
    WORKING = "working"
    HELP = "help"
    IDLE = "idle"
    EXITED = "exited"

    @property
    def wants_attention(self) -> bool:
        """States worth notifying about when work finishes."""
        return self in (Status.HELP, Status.IDLE, Status.EXITED)


@dataclass(frozen=True)
class StatusInfo:
    status: Status
    # Short free-text detail for the sidebar: spinner verb or the question.
    detail: str = ""


_WORKING_MARKER = "esc to interrupt"  # spinner line, present whenever busy

# A selectable option list draws a "❯" cursor before the highlighted entry,
# NUMBERED in permission/trust/plan/question dialogs. The number is required:
# the transcript prefixes every past *user message* with "❯ " too ("❯ hey"),
# which must not read as a dialog. Non-numbered dialogs are caught by their
# confirm-hint footers below.
_HELP_CURSOR = re.compile(r"^\s*❯\s+\d+\.\s", re.MULTILINE)
_HELP_MARKERS = (
    "enter to confirm",
    "do you want",
    "would you like",
)

# Resting input box.
_IDLE_MARKERS = ("│ >", "? for shortcuts")


def classify(visible_text: str, pane_dead: bool = False, changed: bool = False) -> StatusInfo:
    """Classify one poll of a pane.

    `changed` = visible text differs from the previous poll (screen-change
    fallback; pass False if unknown/first poll).
    """
    if pane_dead:
        return StatusInfo(Status.EXITED)
    text = visible_text.rstrip()
    if not text:
        return StatusInfo(Status.STARTING)

    # Dialogs and the input box render near the bottom of the screen; the
    # tail also avoids matching marker-like text quoted in the transcript.
    tail_lines = [ln for ln in text.splitlines() if ln.strip()][-25:]
    tail = "\n".join(tail_lines)
    tail_lower = tail.lower()

    if _WORKING_MARKER in tail_lower:
        return StatusInfo(Status.WORKING, _spinner_detail(tail_lines))
    if _HELP_CURSOR.search(tail) or any(m in tail_lower for m in _HELP_MARKERS):
        return StatusInfo(Status.HELP, _question_detail(tail_lines))
    if any(m in tail for m in _IDLE_MARKERS):
        # Input box with no spinner and no dialog: resting — or the user is
        # typing, which redraws the screen every keystroke, so this must be
        # decided before the screen-change fallback.
        return StatusInfo(Status.IDLE)
    if changed:
        # Unrecognized but actively redrawing (streaming output, unknown
        # spinner style): it's doing something.
        return StatusInfo(Status.WORKING)
    # Static and unrecognized (a /help screen, a changed UI, a plain shell):
    # nothing is running and nothing asks for input — call it idle.
    return StatusInfo(Status.IDLE)


def _spinner_detail(tail_lines: list[str]) -> str:
    """Extract e.g. 'Refactoring…' from the spinner line."""
    for line in reversed(tail_lines):
        if _WORKING_MARKER in line.lower():
            head = line.split("(")[0].strip()
            parts = head.split(None, 1)  # drop the spinner glyph
            if len(parts) == 2 and len(parts[0]) <= 2:
                return parts[1]
            return head
    return ""


def _question_detail(tail_lines: list[str]) -> str:
    """Best-effort: the question being asked, e.g. 'Do you want to ...?'."""
    for line in reversed(tail_lines):
        stripped = line.strip().strip("│").strip()
        if stripped.endswith("?"):
            return stripped[:80]
    return ""

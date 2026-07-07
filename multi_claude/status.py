"""Heuristics that classify a Claude Code pane's state from its visible text.

This is deliberately the only place that knows what Claude Code's TUI looks
like. The markers below are matched against the *visible* pane capture (not
scrollback, which contains stale frames). If Claude Code's UI strings change
in a future version, update the markers here and the unit tests in
tests/test_status.py; misclassification degrades to UNKNOWN, never crashes.

Verified against Claude Code 2.1.x:
- While working it shows a spinner line ending in "(esc to interrupt)".
- Permission / plan-approval / question dialogs render a selectable option
  list whose cursor row is "❯ 1. ..." (often preceded by "Do you want ...").
- At rest, the input box is drawn with box-drawing chars and a "> " prompt
  ("│ > ..."), usually with a "? for shortcuts" hint below it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Status(enum.Enum):
    STARTING = "starting"
    BUSY = "working"
    APPROVAL = "needs input"
    READY = "awaiting message"
    EXITED = "exited"
    UNKNOWN = "unknown"

    @property
    def wants_attention(self) -> bool:
        return self in (Status.APPROVAL, Status.READY, Status.EXITED)


@dataclass(frozen=True)
class StatusInfo:
    status: Status
    # Short free-text detail for the sidebar, e.g. the spinner verb.
    detail: str = ""


_BUSY_MARKER = "esc to interrupt"
_APPROVAL_MARKERS = ("❯ 1.", "> 1.")  # cursor row of an option list
_PROMPT_MARKERS = ("│ >", "? for shortcuts")


def classify(visible_text: str, pane_dead: bool = False) -> StatusInfo:
    if pane_dead:
        return StatusInfo(Status.EXITED)
    text = visible_text.rstrip()
    if not text:
        return StatusInfo(Status.STARTING)

    # Only the tail of the screen matters; dialogs and the input box render
    # at the bottom, and it avoids matching quoted text higher up.
    tail_lines = [ln for ln in text.splitlines() if ln.strip()][-25:]
    tail = "\n".join(tail_lines)

    if _BUSY_MARKER in tail:
        return StatusInfo(Status.BUSY, _spinner_detail(tail_lines))
    if any(m in tail for m in _APPROVAL_MARKERS):
        return StatusInfo(Status.APPROVAL, _question_detail(tail_lines))
    if any(m in tail for m in _PROMPT_MARKERS):
        return StatusInfo(Status.READY)
    return StatusInfo(Status.UNKNOWN)


def _spinner_detail(tail_lines: list[str]) -> str:
    """Extract e.g. 'Hatching… (2m 14s)' from the spinner line."""
    for line in reversed(tail_lines):
        if _BUSY_MARKER in line:
            head = line.split("(")[0].strip()
            # Drop the spinner glyph (first token) if present.
            parts = head.split(None, 1)
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

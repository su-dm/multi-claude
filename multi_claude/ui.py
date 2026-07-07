"""Sidebar UI: a narrow curses pane inside the dashboard tmux window.

The Claude instance displayed on the right is a *real* tmux pane (swapped in
by InstanceManager.display), so this UI only renders the instance list and
handles management keys. Focus moves between sidebar and instance with the
tmux-level Alt-h/Alt-l bindings; while the instance pane is focused, the
user types into Claude directly.

Drawing goes through _addstr, which clips to the window and swallows the
bottom-right-cell curses quirk, so a resize can never crash us.
"""

from __future__ import annotations

import curses
import os
import time

from .config import SIDEBAR_WIDTH
from .manager import InstanceManager, Snapshot
from .status import Status
from .tmux import TmuxError

HELP_LINES = [
    ("j / k", "move selection"),
    ("Enter / l", "show + focus instance"),
    ("1-9", "show instance N"),
    ("M-h / M-l", "focus sidebar / claude"),
    ("M-1..9, M-o", "switch from anywhere"),
    ("M-z", "zoom claude full screen"),
    ("n", "new instance (Tab completes)"),
    ("i", "send a line w/o focusing"),
    ("x", "kill instance (confirms)"),
    ("R", "restart exited instance"),
    ("r", "rename instance"),
    ("q / C-q", "detach (all keeps running)"),
    ("?", "toggle this help"),
]

ICONS = {
    Status.WORKING: "◐",
    Status.IDLE: "●",
    Status.HELP: "◆",
    Status.EXITED: "✖",
    Status.STARTING: "◌",
}


class Sidebar:
    def __init__(self, stdscr: "curses.window", manager: InstanceManager):
        self.scr = stdscr
        self.manager = manager
        self.selected = 0
        self.top = 0  # scroll offset (in instances)
        self.message = "? for help"
        self.message_until = time.monotonic() + 5
        self.show_help = False
        self._init_colors()

    # -- colors ----------------------------------------------------------------

    def _init_colors(self) -> None:
        self.attr: dict[str, int] = {}
        keys = ("working", "idle", "help", "exited", "dim", "accent")
        if not curses.has_colors():
            for key in keys:
                self.attr[key] = curses.A_NORMAL
            return
        curses.start_color()
        curses.use_default_colors()
        pairs = {
            "working": curses.COLOR_CYAN,
            "idle": curses.COLOR_GREEN,
            "help": curses.COLOR_YELLOW,
            "exited": curses.COLOR_RED,
            "dim": 8 if curses.COLORS > 8 else curses.COLOR_WHITE,
            "accent": curses.COLOR_MAGENTA,
        }
        for i, (key, color) in enumerate(pairs.items(), start=1):
            curses.init_pair(i, color, -1)
            self.attr[key] = curses.color_pair(i)

    def _status_attr(self, status: Status) -> int:
        return {
            Status.WORKING: self.attr["working"],
            Status.IDLE: self.attr["idle"],
            Status.HELP: self.attr["help"] | curses.A_BOLD,
            Status.EXITED: self.attr["exited"],
            Status.STARTING: self.attr["dim"],
        }[status]

    # -- safe drawing ------------------------------------------------------------

    def _addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.scr.addnstr(y, x, text, max(0, w - x), attr)
        except curses.error:
            pass  # writing the bottom-right cell always errors; harmless

    # -- main loop ---------------------------------------------------------------

    def run(self) -> None:
        curses.curs_set(0)
        self.scr.timeout(150)
        self.manager.start_polling()
        try:
            while True:
                self._handle_attention()
                self.draw()
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue  # timeout tick; loop redraws with fresh snapshots
                self.handle_key(key)
        finally:
            self.manager.stop_polling()

    def _handle_attention(self) -> None:
        for name, status in self.manager.drain_attention_events():
            self.flash(f"{name}: {status.value}")
            curses.beep()
            self.manager.notify(name, status)

    def flash(self, text: str, seconds: float = 4.0) -> None:
        self.message = text
        self.message_until = time.monotonic() + seconds

    def _enforce_width(self) -> None:
        """Keep the sidebar pane at its fixed width after terminal resizes."""
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            return
        _, w = self.scr.getmaxyx()
        if w != SIDEBAR_WIDTH and not self.manager.tmux.dash_zoomed():
            self.manager.tmux.resize_pane_width(pane, SIDEBAR_WIDTH)

    # -- drawing -----------------------------------------------------------------

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        snaps = self.manager.snapshots()
        self.selected = max(0, min(self.selected, len(snaps) - 1)) if snaps else 0
        self._addstr(0, 0, " multi-claude ".ljust(w), curses.A_BOLD | curses.A_REVERSE)
        if self.show_help:
            self._draw_help(h, w)
            self.scr.refresh()
            return
        self._draw_list(snaps, 1, h - 2, w)
        self._draw_footer(h - 1, w)
        self.scr.refresh()

    def _draw_list(self, snaps: list[Snapshot], y0: int, height: int, width: int) -> None:
        rows_per = 2
        visible = max(1, height // rows_per)
        if self.selected < self.top:
            self.top = self.selected
        if self.selected >= self.top + visible:
            self.top = self.selected - visible + 1
        if not snaps:
            self._addstr(y0 + 1, 1, "no instances yet", self.attr["dim"])
            self._addstr(y0 + 3, 1, "n to create one", curses.A_BOLD)
            return
        for row, idx in enumerate(range(self.top, min(len(snaps), self.top + visible))):
            snap = snaps[idx]
            y = y0 + row * rows_per
            status = snap.status.status
            cursor = "❯" if idx == self.selected else " "
            name_attr = curses.A_BOLD
            if snap.displayed:
                name_attr |= curses.A_REVERSE  # the one on screen right now
            index = f"{idx + 1}" if idx < 9 else "·"
            self._addstr(y, 0, cursor, self.attr["accent"] | curses.A_BOLD)
            self._addstr(y, 2, ICONS[status], self._status_attr(status))
            self._addstr(y, 4, f"{index} {snap.instance.name}"[: width - 5], name_attr)
            detail = snap.status.detail or status.value
            sub = f"{_abbrev_path(snap.instance.cwd)} · {detail}"
            sub_attr = (
                self._status_attr(status)
                if status in (Status.HELP, Status.EXITED)
                else self.attr["dim"]
            )
            self._addstr(y + 1, 4, sub[: width - 5], sub_attr)

    def _draw_footer(self, y: int, w: int) -> None:
        if time.monotonic() < self.message_until and self.message:
            self._addstr(y, 0, f" {self.message} "[: w], self.attr["accent"] | curses.A_BOLD)
            return
        self._addstr(y, 0, " ↵ open · n new · ? help"[: w], self.attr["dim"])

    def _draw_help(self, h: int, w: int) -> None:
        for i, (key, desc) in enumerate(HELP_LINES):
            y = 2 + i * 2
            self._addstr(y, 1, key, curses.A_BOLD)
            self._addstr(y + 1, 3, desc, self.attr["dim"])

    # -- key handling --------------------------------------------------------------

    def handle_key(self, key) -> None:
        if self.show_help:
            self.show_help = False
            return
        snaps = self.manager.snapshots()
        n = len(snaps)
        if key == curses.KEY_RESIZE:
            self._enforce_width()
            return
        if isinstance(key, str):
            if key == "q":
                self.manager.tmux.detach_dashboard_clients()
            elif key == "?":
                self.show_help = True
            elif key == "j":
                self.selected = min(n - 1, self.selected + 1) if n else 0
            elif key == "k":
                self.selected = max(0, self.selected - 1)
            elif key == "g":
                self.selected = 0
            elif key == "G":
                self.selected = max(0, n - 1)
            elif key.isdigit() and key != "0":
                if int(key) <= n:
                    self.selected = int(key) - 1
                    self.action_display(self.manager.snapshots())
            elif key in ("\n", "\r", "l", "o"):
                self.action_display(snaps)
            elif key == "n":
                self.action_new()
            elif key == "i":
                self.action_send(snaps)
            elif key == "x":
                self.action_kill(snaps)
            elif key == "R":
                self.action_restart(snaps)
            elif key == "r":
                self.action_rename(snaps)
        elif key == curses.KEY_DOWN:
            self.selected = min(n - 1, self.selected + 1) if n else 0
        elif key == curses.KEY_UP:
            self.selected = max(0, self.selected - 1)
        elif key == curses.KEY_ENTER:
            self.action_display(snaps)

    # -- actions ---------------------------------------------------------------------

    def _current(self, snaps: list[Snapshot]) -> Snapshot | None:
        if not snaps:
            self.flash("no instances — n creates one")
            return None
        return snaps[self.selected]

    def action_display(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        try:
            self.manager.display(snap.instance.name)
        except (KeyError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=6)

    def action_new(self) -> None:
        cwd = self.prompt(
            "directory", initial=os.path.expanduser("~/"), completer=complete_dir
        )
        if cwd is None:
            return
        default_name = os.path.basename(
            os.path.abspath(os.path.expanduser(cwd.strip() or "~"))
        )
        name = self.prompt("name", initial=default_name)
        if name is None:
            return
        try:
            inst = self.manager.create(cwd.strip(), name.strip() or None)
        except (ValueError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)
            return
        self.selected = max(0, len(self.manager.registry.instances) - 1)
        try:
            self.manager.display(inst.name, focus=False)
        except (KeyError, TmuxError):
            pass
        self.flash(f"created {inst.name}")

    def action_send(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        text = self.prompt(f"send to {snap.instance.name}")
        if text is None or not text.strip():
            return
        try:
            self.manager.send_text(snap.instance.name, text)
            self.flash(f"sent to {snap.instance.name}")
        except (KeyError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)

    def action_kill(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        if not self.confirm(f"kill {snap.instance.name}? y/N"):
            return
        try:
            self.manager.kill(snap.instance.name)
        except (KeyError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)
            return
        self.flash(f"killed {snap.instance.name}")
        self.selected = max(0, self.selected - 1)

    def action_restart(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        if snap.status.status is not Status.EXITED:
            self.flash("still running (R is for exited)")
            return
        try:
            self.manager.restart(snap.instance.name)
            self.flash(f"restarted {snap.instance.name}")
        except (KeyError, ValueError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)

    def action_rename(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        new = self.prompt("new name", initial=snap.instance.name)
        if new is None or not new.strip():
            return
        try:
            final = self.manager.rename(snap.instance.name, new.strip())
            self.flash(f"renamed to {final}")
        except (KeyError, ValueError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)

    # -- modal input --------------------------------------------------------------------

    def prompt(self, label: str, initial: str = "", completer=None) -> str | None:
        """Single-line editor on the footer row. Enter accepts, ESC cancels,
        Tab completes (when a completer is given), C-u clears, C-w kills a
        word. Candidates show on the row above the input."""
        h, w = self.scr.getmaxyx()
        buf = list(initial)
        hint = ""
        curses.curs_set(1)
        self.scr.timeout(-1)  # block while editing
        try:
            while True:
                prefix = f" {label}: "
                text = "".join(buf)
                avail = max(1, w - len(prefix) - 2)
                shown = text[-avail:]
                self._addstr(h - 2, 0, " " * (w - 1))
                if hint:
                    self._addstr(h - 2, 0, f" {hint}"[: w - 1], self.attr["dim"])
                self._addstr(h - 1, 0, " " * (w - 1))
                self._addstr(h - 1, 0, prefix, curses.A_BOLD)
                self._addstr(h - 1, len(prefix), shown)
                self.scr.move(h - 1, min(w - 2, len(prefix) + len(shown)))
                self.scr.refresh()
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf)
                    if key == "\x1b":  # ESC
                        return None
                    if key == "\t" and completer is not None:
                        completed, candidates = completer("".join(buf))
                        buf = list(completed)
                        if len(candidates) > 1:
                            hint = "  ".join(candidates)[: 3 * w]
                        elif not candidates:
                            hint = "(no match)"
                        else:
                            hint = ""
                        continue
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":  # C-u: clear line
                        buf.clear()
                    elif key == "\x17":  # C-w: delete word back
                        while buf and buf[-1] == " ":
                            buf.pop()
                        while buf and buf[-1] not in (" ", "/"):
                            buf.pop()
                    elif key.isprintable():
                        buf.append(key)
                    hint = ""
                elif key == curses.KEY_BACKSPACE:
                    if buf:
                        buf.pop()
                    hint = ""
                elif key == curses.KEY_RESIZE:
                    self._enforce_width()
                    h, w = self.scr.getmaxyx()
        finally:
            curses.curs_set(0)
            self.scr.timeout(150)
            self.draw()

    def confirm(self, question: str) -> bool:
        h, w = self.scr.getmaxyx()
        self._addstr(h - 1, 0, " " * (w - 1))
        self._addstr(h - 1, 0, f" {question} "[: w - 1], self.attr["help"] | curses.A_BOLD)
        self.scr.refresh()
        self.scr.timeout(-1)
        try:
            while True:
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    return key.lower() == "y"
                if key == curses.KEY_RESIZE:
                    continue
                return False
        finally:
            self.scr.timeout(150)


def complete_dir(text: str) -> tuple[str, list[str]]:
    """Tab completion for directory paths. Returns (new_text, candidates).

    Completes to the longest common prefix of matching directories; a unique
    match gains a trailing '/'. Hidden directories only match when the
    fragment itself starts with '.'. A leading '~' is preserved in what the
    user sees.
    """
    raw = text.strip() or "~/"
    expanded = os.path.expanduser(raw)
    if raw.endswith("/"):
        base, frag = expanded.rstrip("/") or "/", ""
    else:
        base, frag = os.path.split(expanded)
        base = base or "."
    try:
        entries = sorted(
            e
            for e in os.listdir(base)
            if e.startswith(frag)
            and os.path.isdir(os.path.join(base, e))
            and (frag.startswith(".") or not e.startswith("."))
        )
    except OSError:
        return text, []
    if not entries:
        return text, []
    common = os.path.commonprefix(entries)
    completed = os.path.join(base, common)
    if len(entries) == 1:
        completed += "/"
    home = os.path.expanduser("~")
    if raw.startswith("~") and completed.startswith(home):
        completed = "~" + completed[len(home):]
    return completed, entries


def _abbrev_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path


def run_sidebar(manager: InstanceManager) -> None:
    curses.wrapper(lambda scr: Sidebar(scr, manager).run())

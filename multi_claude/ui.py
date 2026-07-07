"""Curses dashboard: sidebar of instances + live preview + vim-ish keys.

Rendering rules:
- The UI thread never calls tmux directly for polling; it reads snapshots
  from InstanceManager (background thread) and only shells out for direct
  user actions (create/kill/send/attach).
- Every draw goes through _addstr, which clips to the window and swallows
  the bottom-right-cell curses quirk, so a resize can never crash us.
"""

from __future__ import annotations

import curses
import os
import time

from .manager import InstanceManager, Snapshot
from .status import Status
from .tmux import TmuxError

SIDEBAR_MIN = 26
SIDEBAR_MAX = 40

HELP_LINES = [
    ("j / k, ↓ / ↑", "move selection"),
    ("g / G", "first / last instance"),
    ("1-9", "jump to instance"),
    ("Enter / l / o", "attach (C-q inside to come back)"),
    ("n", "new instance (asks for directory)"),
    ("i", "send a message without attaching"),
    ("x", "kill instance (confirms)"),
    ("R", "restart an exited instance"),
    ("r", "rename instance"),
    ("q", "quit dashboard (instances keep running)"),
    ("?", "toggle this help"),
]

ICONS = {
    Status.BUSY: "◐",
    Status.READY: "●",
    Status.APPROVAL: "◆",
    Status.EXITED: "✖",
    Status.STARTING: "◌",
    Status.UNKNOWN: "○",
}


class Dashboard:
    def __init__(self, stdscr: "curses.window", manager: InstanceManager):
        self.scr = stdscr
        self.manager = manager
        self.selected = 0
        self.top = 0  # sidebar scroll offset (in instances)
        self.message = "press ? for help"
        self.message_until = time.monotonic() + 5
        self.show_help = False
        self._init_colors()

    # -- colors --------------------------------------------------------------

    def _init_colors(self) -> None:
        self.attr: dict[str, int] = {}
        if not curses.has_colors():
            for key in ("busy", "ready", "approval", "exited", "dim", "accent"):
                self.attr[key] = curses.A_NORMAL
            return
        curses.start_color()
        curses.use_default_colors()
        pairs = {
            "busy": curses.COLOR_CYAN,
            "ready": curses.COLOR_GREEN,
            "approval": curses.COLOR_YELLOW,
            "exited": curses.COLOR_RED,
            "dim": 8 if curses.COLORS > 8 else curses.COLOR_WHITE,
            "accent": curses.COLOR_MAGENTA,
        }
        for i, (key, color) in enumerate(pairs.items(), start=1):
            curses.init_pair(i, color, -1)
            self.attr[key] = curses.color_pair(i)

    def _status_attr(self, status: Status) -> int:
        return {
            Status.BUSY: self.attr["busy"],
            Status.READY: self.attr["ready"],
            Status.APPROVAL: self.attr["approval"] | curses.A_BOLD,
            Status.EXITED: self.attr["exited"],
            Status.STARTING: self.attr["dim"],
            Status.UNKNOWN: self.attr["dim"],
        }[status]

    # -- safe drawing ----------------------------------------------------------

    def _addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.scr.addnstr(y, x, text, max(0, w - x), attr)
        except curses.error:
            pass  # writing the bottom-right cell always errors; harmless

    # -- main loop -------------------------------------------------------------

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
                if not self.handle_key(key):
                    return
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

    # -- drawing -----------------------------------------------------------

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 8 or w < 40:
            self._addstr(0, 0, "window too small for multi-claude")
            self.scr.refresh()
            return
        snaps = self.manager.snapshots()
        self.selected = max(0, min(self.selected, len(snaps) - 1)) if snaps else 0
        sidebar_w = max(SIDEBAR_MIN, min(SIDEBAR_MAX, w // 3))
        body_h = h - 2  # minus title row and footer row

        self._draw_title(w)
        self._draw_sidebar(snaps, 1, body_h, sidebar_w)
        for y in range(1, h - 1):
            self._addstr(y, sidebar_w, "│", self.attr["dim"])
        self._draw_preview(snaps, 1, body_h, sidebar_w + 2, w - sidebar_w - 2)
        self._draw_footer(h - 1, w)
        if self.show_help:
            self._draw_help(h, w)
        self.scr.refresh()

    def _draw_title(self, w: int) -> None:
        self._addstr(0, 0, " multi-claude ", curses.A_BOLD | curses.A_REVERSE)
        self._addstr(0, 15, f"{len(self.manager.registry.instances)} instance(s)", self.attr["dim"])

    def _draw_sidebar(self, snaps: list[Snapshot], y0: int, height: int, width: int) -> None:
        rows_per = 2
        visible = max(1, height // rows_per)
        if self.selected < self.top:
            self.top = self.selected
        if self.selected >= self.top + visible:
            self.top = self.selected - visible + 1
        if not snaps:
            self._addstr(y0 + 1, 1, "no instances yet", self.attr["dim"])
            self._addstr(y0 + 3, 1, "press n to create one", self.attr["dim"] | curses.A_BOLD)
            return
        for row, idx in enumerate(range(self.top, min(len(snaps), self.top + visible))):
            snap = snaps[idx]
            y = y0 + row * rows_per
            status = snap.status.status
            selected = idx == self.selected
            line_attr = curses.A_REVERSE if selected else 0
            icon_attr = self._status_attr(status) | (curses.A_REVERSE if selected else 0)
            index = f"{idx + 1}" if idx < 9 else " "
            if selected:
                self._addstr(y, 0, " " * width, line_attr)
            self._addstr(y, 1, ICONS[status], icon_attr)
            self._addstr(y, 3, f"{index} {snap.instance.name}"[: width - 4], line_attr | curses.A_BOLD)
            detail = snap.status.detail or status.value
            sub = f"{_abbrev_path(snap.instance.cwd)} · {detail}"
            sub_attr = self._status_attr(status) if status.wants_attention else self.attr["dim"]
            self._addstr(y + 1, 3, sub[: width - 4], sub_attr)

    def _draw_preview(self, snaps: list[Snapshot], y0: int, height: int, x0: int, width: int) -> None:
        if not snaps:
            return
        snap = snaps[self.selected]
        status = snap.status.status
        header = f"{snap.instance.name} — {snap.instance.cwd}"
        self._addstr(y0, x0, header[:width], curses.A_BOLD)
        self._addstr(
            y0 + 1, x0,
            f"[{status.value}{': ' + snap.status.detail if snap.status.detail else ''}]"[:width],
            self._status_attr(status),
        )
        body_y = y0 + 3
        body_h = height - 3
        if not snap.session_alive:
            self._addstr(body_y, x0, "session has terminated — R to restart, x to remove", self.attr["dim"])
            return
        lines = [ln.rstrip() for ln in snap.preview.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        for i, line in enumerate(lines[-body_h:]):
            self._addstr(body_y + i, x0, line[:width])

    def _draw_footer(self, y: int, w: int) -> None:
        if time.monotonic() < self.message_until and self.message:
            self._addstr(y, 0, f" {self.message} ", self.attr["accent"] | curses.A_BOLD)
            return
        keys = " j/k move · Enter attach · n new · i send · x kill · R restart · r rename · ? help · q quit"
        self._addstr(y, 0, keys[: w - 1], self.attr["dim"])

    def _draw_help(self, h: int, w: int) -> None:
        box_w = min(64, w - 4)
        box_h = len(HELP_LINES) + 4
        y0, x0 = max(1, (h - box_h) // 2), max(2, (w - box_w) // 2)
        for y in range(y0, y0 + box_h):
            self._addstr(y, x0, " " * box_w, curses.A_REVERSE)
        self._addstr(y0 + 1, x0 + 2, "multi-claude keys", curses.A_REVERSE | curses.A_BOLD)
        for i, (key, desc) in enumerate(HELP_LINES):
            self._addstr(y0 + 3 + i, x0 + 2, f"{key:<16} {desc}"[: box_w - 4], curses.A_REVERSE)

    # -- key handling --------------------------------------------------------

    def handle_key(self, key) -> bool:
        """Returns False when the dashboard should exit."""
        if self.show_help:
            self.show_help = False
            return True
        snaps = self.manager.snapshots()
        n = len(snaps)
        if key == curses.KEY_RESIZE:
            return True
        if isinstance(key, str):
            if key == "q":
                return False
            if key == "?":
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
            elif key in ("\n", "\r", "l", "o"):
                self.action_attach(snaps)
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
            self.action_attach(snaps)
        return True

    # -- actions -------------------------------------------------------------

    def _current(self, snaps: list[Snapshot]) -> Snapshot | None:
        if not snaps:
            self.flash("no instances — press n to create one")
            return None
        return snaps[self.selected]

    def action_attach(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        if not snap.session_alive:
            self.flash("session terminated — R to restart it first")
            return
        # Hand the terminal to tmux; curses resumes on the next refresh.
        curses.endwin()
        try:
            self.manager.attach(snap.instance.name)
        finally:
            self.scr.refresh()
            curses.curs_set(0)
            curses.flushinp()
        self.manager.poll_once()

    def action_new(self) -> None:
        cwd = self.prompt("directory for new instance", initial=os.path.expanduser("~/"))
        if cwd is None:
            return
        default_name = os.path.basename(os.path.abspath(os.path.expanduser(cwd.strip() or "~")))
        name = self.prompt("instance name", initial=default_name)
        if name is None:
            return
        try:
            inst = self.manager.create(cwd.strip(), name.strip() or None)
        except (ValueError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)
            return
        self.flash(f"created {inst.name}")
        self.selected = max(0, len(self.manager.registry.instances) - 1)

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
        except TmuxError as exc:
            self.flash(f"error: {exc}", seconds=8)

    def action_kill(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        if not self.confirm(f"kill {snap.instance.name}? (y/N)"):
            return
        self.manager.kill(snap.instance.name)
        self.flash(f"killed {snap.instance.name}")
        self.selected = max(0, self.selected - 1)

    def action_restart(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        if snap.status.status is not Status.EXITED:
            self.flash("instance is still running (restart is for exited ones)")
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

    # -- modal input ---------------------------------------------------------

    def prompt(self, label: str, initial: str = "") -> str | None:
        """Single-line editor on the footer row. Enter accepts, ESC cancels."""
        h, w = self.scr.getmaxyx()
        buf = list(initial)
        curses.curs_set(1)
        self.scr.timeout(-1)  # block while editing
        try:
            while True:
                prefix = f" {label}: "
                text = "".join(buf)
                avail = max(1, w - len(prefix) - 2)
                shown = text[-avail:]
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
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":  # C-u: clear line
                        buf.clear()
                    elif key == "\x17":  # C-w: delete word back
                        while buf and buf[-1] == " ":
                            buf.pop()
                        while buf and buf[-1] != " ":
                            buf.pop()
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE,):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_RESIZE:
                    h, w = self.scr.getmaxyx()
        finally:
            curses.curs_set(0)
            self.scr.timeout(150)

    def confirm(self, question: str) -> bool:
        h, w = self.scr.getmaxyx()
        self._addstr(h - 1, 0, " " * (w - 1))
        self._addstr(h - 1, 0, f" {question} ", self.attr["approval"] | curses.A_BOLD)
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


def _abbrev_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path


def run_dashboard(manager: InstanceManager) -> None:
    curses.wrapper(lambda scr: Dashboard(scr, manager).run())

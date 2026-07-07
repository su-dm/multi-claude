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
import shlex
import shutil
import sys
import textwrap
import time

from . import CLAUDE_CODE_VERIFIED
from .manager import InstanceManager, Snapshot
from .status import Status
from .tmux import TmuxError
from .transcripts import fmt_cost, fmt_model, fmt_tokens

# Alt on Linux; on macOS the same bindings are the Option key (the letter
# ones also work when the terminal types ˙/¬/Ω/ø/å instead of sending Meta).
_MAC = sys.platform == "darwin"
_M = "⌥" if _MAC else "M-"

HELP_LINES = [
    ("j / k", "move selection"),
    ("Enter / l", "show + focus instance"),
    ("1-9", "show instance N"),
    (f"{_M}h / {_M}l", "focus sidebar / claude"),
    (f"{_M}1..9, {_M}o", "switch from anywhere"),
    *(
        [
            ("", "  (⌥ keys need Option set to"),
            ("", "  Esc+/Meta in your terminal's"),
            ("", "  key settings, see bottom;"),
            ("", "  C-b o also switches panes)"),
        ]
        if _MAC
        else []
    ),
    (f"a / {_M}a", "jump to agent needing input"),
    (f"{_M}z", "zoom claude full screen"),
    ("v", "toggle compact/expanded view"),
    ("< / >", "narrow / widen the sidebar (persisted)"),
    ("n", "new instance (Tab completes;"),
    ("", "  offers git worktree isolation)"),
    ("i", "send a line w/o focusing"),
    ("p", "pin/unpin to top"),
    ("d", "archive (hide, revivable)"),
    ("A", "show/hide archived"),
    ("N", "toggle notifications"),
    ("x", "kill instance (confirms)"),
    ("R", "restart exited (fresh)"),
    ("C", "resume exited (continue convo)"),
    ("r", "rename instance"),
    ("c", "open claude configs/skills"),
    ("S", "agent: condense work to skill"),
    ("H", "agent: write HANDOFF.md"),
    ("q / C-q", "detach (all keeps running)"),
    ("C-c", "quit dashboard (agents keep running;"),
    ("", "  also works on a dead agent pane)"),
    ("?", "this help (q closes)"),
]


def help_text() -> str:
    """The key reference as plain text for the tmux popup renderer."""
    out = ["", "  multi-claude keys", "  " + "─" * 17, ""]
    for key, desc in HELP_LINES:
        out.append(f"  {key:<12} {desc}")
    if _MAC:
        out += [
            "",
            "  ⌥ keys: set Option to send Esc+/Meta, or the",
            "  ⌥ combos type accents instead of reaching tmux:",
            "    iTerm2:   Settings → Profiles → Keys → General",
            "              → Left Option key: Esc+",
            "    Terminal: Settings → Profiles → Keyboard",
            "              → Use Option as Meta key",
        ]
    out += ["", "  reopen a closed dashboard with: multi-claude", ""]
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    """Wrap text to width, never dropping content (long words break)."""
    return textwrap.wrap(
        text, max(1, width), break_long_words=True, break_on_hyphens=False
    )

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
        self.expanded = False
        self.show_archived = False
        self.help_top = 0
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

    def _token_attr(self, tokens: int | None) -> int:
        """Context-size coloring; thresholds assume the common 200k window
        (informational only — actual limits vary by model)."""
        if tokens is None:
            return self.attr["dim"]
        if tokens >= 180_000:
            return self.attr["exited"] | curses.A_BOLD
        if tokens >= 150_000:
            return self.attr["help"]
        return self.attr["dim"]

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

    def run(self) -> bool:
        """Event loop. Returns True when the user hit C-c: the caller must
        then shut the whole dashboard down (after curses has been torn down,
        so the terminal is restored first).

        The try covers startup too — claude_code_series() forks `claude
        --version`, which can take seconds, and a C-c landing there must
        still quit gracefully rather than leave a dead frozen pane."""
        try:
            curses.curs_set(0)
            self.scr.timeout(150)
            series = self.manager.config.claude_code_series()
            if series and series != CLAUDE_CODE_VERIFIED:
                self.flash(
                    f"claude code {series}.x untested (verified {CLAUDE_CODE_VERIFIED}.x)",
                    seconds=10,
                )
            self.manager.start_polling()
            while True:
                self._handle_attention()
                self.draw()
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue  # timeout tick; loop redraws with fresh snapshots
                self.handle_key(key)
        except KeyboardInterrupt:
            # C-c anywhere in the sidebar (including inside prompt/pick/
            # confirm): graceful quit.
            return True
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
        """Keep the sidebar pane at its configured width after terminal
        resizes (the width itself is adjustable with < / >)."""
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            return
        want = self.manager.config.sidebar_width
        _, w = self.scr.getmaxyx()
        if w != want and not self.manager.tmux.dash_zoomed():
            self.manager.tmux.resize_pane_width(pane, want)

    # -- drawing -----------------------------------------------------------------

    def draw(self) -> None:
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        snaps = self.manager.snapshots(include_archived=self.show_archived)
        self.selected = max(0, min(self.selected, len(snaps) - 1)) if snaps else 0
        self._addstr(0, 0, " multi-claude ".ljust(w), curses.A_BOLD | curses.A_REVERSE)
        if self.show_help:
            self._draw_help(h, w)
            self.scr.refresh()
            return
        footer, footer_attr = self._footer_lines(w)
        self._draw_list(snaps, 1, h - 1 - len(footer), w)
        for i, line in enumerate(footer):
            self._addstr(h - len(footer) + i, 0, f" {line}"[: w - 1], footer_attr)
        self.scr.refresh()

    def _rows_per_item(self) -> int:
        return 4 if self.expanded else 2

    def _draw_list(self, snaps: list[Snapshot], y0: int, height: int, width: int) -> None:
        rows_per = self._rows_per_item()
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
            tokens = fmt_tokens(snap.tokens)
            pin = "✦" if snap.instance.pinned else ""
            if snap.instance.archived:
                name_attr = self.attr["dim"]
            name_room = width - 5 - (len(tokens) + 1 if tokens else 0)
            self._addstr(y, 0, cursor, self.attr["accent"] | curses.A_BOLD)
            self._addstr(y, 2, ICONS[status], self._status_attr(status))
            self._addstr(y, 4, f"{index} {pin}{snap.instance.name}"[:name_room], name_attr)
            if tokens:
                self._addstr(y, width - len(tokens) - 1, tokens, self._token_attr(snap.tokens))
            detail = "archived" if snap.instance.archived else (snap.status.detail or status.value)
            sub = f"{_abbrev_path(snap.instance.cwd)} · {detail}"
            sub_attr = (
                self._status_attr(status)
                if status in (Status.HELP, Status.EXITED)
                else self.attr["dim"]
            )
            self._addstr(y + 1, 4, sub[: width - 5], sub_attr)
            if self.expanded:
                self._draw_expanded_rows(snap, y, width)

    def _draw_expanded_rows(self, snap: Snapshot, y: int, width: int) -> None:
        """Rows 3-4 of an expanded item: model · cost · git, then activity."""
        facts = []
        sess = snap.session
        if sess and sess.model:
            facts.append(fmt_model(sess.model))
        if sess and sess.cost_usd is not None:
            approx = "~" if sess.cost_source == "estimate" else ""
            facts.append(f"{approx}{fmt_cost(sess.cost_usd)}")
        if snap.git:
            facts.append(snap.git.summary())
        self._addstr(y + 2, 4, " · ".join(facts)[: width - 5], self.attr["dim"])
        activity = ""
        if snap.status.status is Status.WORKING:
            # Prefer the live transcript line over the screen-scraped spinner.
            activity = (sess.activity if sess else "") or snap.status.detail
        elif sess:
            activity = sess.activity or sess.title
        self._addstr(y + 3, 4, activity[: width - 5], self.attr["accent"])

    def _footer_lines(self, w: int) -> tuple[list[str], int]:
        """Footer rows (bottom of the sidebar): a flash message wraps over up
        to 4 rows so it's never cut off; otherwise the standing hint."""
        if time.monotonic() < self.message_until and self.message:
            lines = _wrap(self.message, w - 2)[:4] or [""]
            return lines, self.attr["accent"] | curses.A_BOLD
        return ["↵ open · n new · ? help"], self.attr["dim"]

    def _help_rows(self, w: int) -> list[tuple[str, str]]:
        """HELP_LINES with descriptions wrapped to the current width, so the
        fallback (non-popup) help never truncates."""
        rows: list[tuple[str, str]] = []
        for key, desc in HELP_LINES:
            wrapped = _wrap(desc, max(6, w - 15)) or [""]
            rows.append((key, wrapped[0]))
            rows.extend(("", cont) for cont in wrapped[1:])
        return rows

    def _draw_help(self, h: int, w: int) -> None:
        rows = self._help_rows(w)
        visible = h - 3
        self.help_top = max(0, min(self.help_top, len(rows) - visible))
        for row, (key, desc) in enumerate(rows[self.help_top : self.help_top + visible]):
            y = 2 + row
            self._addstr(y, 1, f"{key:<12}"[:13], curses.A_BOLD)
            self._addstr(y, 14, desc, self.attr["dim"])
        more = len(rows) - self.help_top - visible
        hint = f"j/k scroll ({more} more) · any key closes" if more > 0 else "any key closes"
        self._addstr(h - 1, 0, f" {hint}"[: w - 1], self.attr["accent"])

    # -- key handling --------------------------------------------------------------

    def handle_key(self, key) -> None:
        if self.show_help:
            if key in ("j", curses.KEY_DOWN):
                self.help_top += 1
            elif key in ("k", curses.KEY_UP):
                self.help_top = max(0, self.help_top - 1)
            else:
                self.show_help = False
                self.help_top = 0
            return
        snaps = self.manager.snapshots(include_archived=self.show_archived)
        n = len(snaps)
        if key == curses.KEY_RESIZE:
            self._enforce_width()
            return
        if isinstance(key, str):
            if key == "q":
                self.manager.tmux.detach_dashboard_clients()
            elif key == "?":
                self.action_help()
            elif key in ("<", ">"):
                self.action_resize(2 if key == ">" else -2)
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
                self.action_restart(snaps, resume=False)
            elif key == "C":
                self.action_restart(snaps, resume=True)
            elif key == "r":
                self.action_rename(snaps)
            elif key == "a":
                self.manager.select("attention")
            elif key == "v":
                self.expanded = not self.expanded
                self.top = 0
            elif key == "N":
                on = self.manager.toggle_notify()
                self.flash("notifications " + ("ON" if on else "OFF"))
            elif key == "A":
                self.show_archived = not self.show_archived
                self.top = 0
                self.flash("showing archived" if self.show_archived else "hiding archived")
            elif key == "p":
                self.action_pin(snaps)
            elif key == "d":
                self.action_archive(snaps)
            elif key == "c":
                self.action_config()
            elif key == "S":
                self.action_invoke_skill(
                    snaps, "condense-to-skill",
                    "Use the condense-to-skill skill: condense the non-obvious "
                    "work from this session into a reusable skill file and save it.",
                )
            elif key == "H":
                self.action_invoke_skill(
                    snaps, "handoff",
                    "Use the handoff skill: write or update the project's "
                    "HANDOFF.md progress file for a fresh session to continue from.",
                )
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

    def action_help(self) -> None:
        """Key reference in a centered tmux popup (scrolls via less); falls
        back to the in-sidebar overlay on tmux < 3.2 (no display-popup)."""
        if not self.manager.tmux.supports_popup():
            self.show_help = True
            return
        cmd = f"{self.manager.config.mc_command()} help-popup"
        if shutil.which("less"):
            cmd += " | less -R -Ps" + shlex.quote("j/k scroll · q closes")
        lines = help_text().count("\n") + 2
        ww, wh = self.manager.tmux.dash_size()
        width = max(30, min(56, ww - 4)) if ww else 56
        height = max(10, min(lines, wh - 2)) if wh else lines
        self.manager.tmux.popup(cmd, width=str(width), height=str(height))

    def action_resize(self, delta: int) -> None:
        """Adjust + persist the sidebar width (< narrower, > wider)."""
        cfg = self.manager.config
        cfg.sidebar_width = max(24, min(100, cfg.sidebar_width + delta))
        cfg.save_setting("sidebar_width", cfg.sidebar_width)
        self._enforce_width()
        self.flash(f"sidebar width {cfg.sidebar_width}")

    def action_display(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        try:
            self.manager.display(snap.instance.name)
        except (KeyError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=6)

    def _agents_in(self, cwd: str) -> list[str]:
        """Names of live (non-archived) agents working in this directory or
        anywhere in the same git repo (incl. its worktrees' parent repo)."""
        target = os.path.abspath(os.path.expanduser(cwd or "~"))
        target_top = self.manager.repo_top(target) or target
        names = []
        for inst in self.manager.registry.ordered():
            inst_top = self.manager.repo_top(inst.cwd) or inst.cwd
            if inst.cwd == target or inst_top == target_top:
                names.append(inst.name)
        return names

    def action_new(self) -> None:
        cwd = self.prompt(
            "directory", initial=os.path.expanduser("~/"), completer=complete_dir
        )
        if cwd is None:
            return
        cwd = cwd.strip()
        worktree_branch = None
        others = self._agents_in(cwd)
        if self.manager.is_git_dir(cwd):
            if others:
                title = f"⚠ {', '.join(others[:3])} already here — isolate?"
                options = [
                    ("isolated worktree + branch (recommended)", "worktree"),
                    ("shared checkout (agents may collide!)", "shared"),
                ]
            else:
                title = "git repo — where should this agent work?"
                options = [
                    ("shared checkout (touch the same files)", "shared"),
                    ("isolated worktree + branch", "worktree"),
                ]
            mode = self.pick(title, options)
            if mode is None:
                return
            if mode == "worktree":
                worktree_branch = self.prompt("branch name", initial="agent/")
                if worktree_branch is None or not worktree_branch.strip():
                    return
                worktree_branch = worktree_branch.strip()
        elif others:
            # Not a git repo, so worktree isolation isn't possible — say so
            # instead of silently letting agents collide.
            if not self.confirm(
                f"{others[0]} already works here (no git → no worktree) — share dir? y/N"
            ):
                return
        default_name = worktree_branch.split("/")[-1] if worktree_branch else os.path.basename(
            os.path.abspath(os.path.expanduser(cwd or "~"))
        )
        name = self.prompt("name", initial=default_name)
        if name is None:
            return
        try:
            inst = self.manager.create(
                cwd, name.strip() or None, worktree_branch=worktree_branch
            )
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

    def action_restart(self, snaps: list[Snapshot], resume: bool = False) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        if snap.instance.archived:
            try:
                self.manager.unarchive(snap.instance.name, resume=resume)
                self.flash(f"revived {snap.instance.name}")
            except (KeyError, ValueError, TmuxError) as exc:
                self.flash(f"error: {exc}", seconds=8)
            return
        if snap.status.status is not Status.EXITED:
            self.flash("still running (R/C are for exited)")
            return
        try:
            self.manager.restart(snap.instance.name, resume=resume)
            verb = "resumed" if resume else "restarted"
            self.flash(f"{verb} {snap.instance.name}")
        except (KeyError, ValueError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)

    def action_pin(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        try:
            pinned = self.manager.toggle_pin(snap.instance.name)
            self.flash(("pinned " if pinned else "unpinned ") + snap.instance.name)
            self.selected = 0 if pinned else self.selected
        except KeyError as exc:
            self.flash(f"error: {exc}", seconds=8)

    def action_archive(self, snaps: list[Snapshot]) -> None:
        snap = self._current(snaps)
        if snap is None:
            return
        if snap.instance.archived:
            # Archived + d = revive.
            try:
                self.manager.unarchive(snap.instance.name)
                self.flash(f"revived {snap.instance.name}")
            except (KeyError, ValueError, TmuxError) as exc:
                self.flash(f"error: {exc}", seconds=8)
            return
        if snap.status.status is Status.WORKING:
            if not self.confirm(f"{snap.instance.name} is working — archive anyway? y/N"):
                return
        try:
            self.manager.archive(snap.instance.name)
            self.flash(f"archived {snap.instance.name} (A shows archived, d revives)")
            self.selected = max(0, self.selected - 1)
        except (KeyError, TmuxError) as exc:
            self.flash(f"error: {exc}", seconds=8)

    def action_config(self) -> None:
        """Pick a claude config file/dir and open it in $EDITOR via a tmux
        popup over the dashboard. Project entries come from the selected
        instance's directory."""
        snaps = self.manager.snapshots()
        cwd = snaps[self.selected].instance.cwd if snaps else os.path.expanduser("~")
        home = str(self.manager.config.claude_home)
        entries = [
            ("global CLAUDE.md", os.path.join(home, "CLAUDE.md")),
            ("global settings.json", os.path.join(home, "settings.json")),
            ("global skills/", os.path.join(home, "skills")),
            ("global agents/", os.path.join(home, "agents")),
            ("project CLAUDE.md", os.path.join(cwd, "CLAUDE.md")),
            ("project settings.json", os.path.join(cwd, ".claude", "settings.json")),
            ("project settings.local.json", os.path.join(cwd, ".claude", "settings.local.json")),
            ("project skills/", os.path.join(cwd, ".claude", "skills")),
        ]
        target = self.pick("open config", [
            (label + ("" if os.path.exists(path) else "  (new)"), path)
            for label, path in entries
        ])
        if target is None:
            return
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        if os.path.isdir(target):
            # Directory (skills/agents): open the editor's file browser there.
            cmd = f"cd {shlex.quote(target)} && {editor} ."
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            cmd = f"{editor} {shlex.quote(target)}"
        self.manager.tmux.popup(cmd)

    def action_invoke_skill(self, snaps: list[Snapshot], skill: str, prompt: str) -> None:
        """Send a skill invocation to the selected agent (typed as a message,
        so it works exactly like the user asking for it)."""
        snap = self._current(snaps)
        if snap is None:
            return
        skill_dir = self.manager.config.claude_home / "skills" / skill
        if not (skill_dir / "SKILL.md").exists():
            self.flash(f"missing skill: {skill_dir}", seconds=8)
            return
        if snap.status.status is Status.WORKING:
            if not self.confirm(f"{snap.instance.name} is working — send anyway? y/N"):
                return
        try:
            self.manager.send_text(snap.instance.name, prompt)
            self.flash(f"asked {snap.instance.name} to run {skill}")
        except (KeyError, TmuxError) as exc:
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

    def pick(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Full-sidebar list picker: j/k move, Enter accepts, ESC/q cancels.
        Returns the selected option's value."""
        sel = 0
        self.scr.timeout(-1)
        try:
            while True:
                self.scr.erase()
                _, w = self.scr.getmaxyx()
                y = 0
                for line in _wrap(title, w - 2) or [title]:
                    self._addstr(y, 0, f" {line} ".ljust(w), curses.A_BOLD | curses.A_REVERSE)
                    y += 1
                y += 1
                for i, (label, _) in enumerate(options):
                    marker = "❯ " if i == sel else "  "
                    attr = curses.A_BOLD if i == sel else self.attr["dim"]
                    lines = _wrap(label, w - 4) or [label]
                    self._addstr(y, 1, marker + lines[0], attr)
                    for cont in lines[1:]:
                        y += 1
                        self._addstr(y, 3, cont, attr)
                    y += 1
                self._addstr(y + 1, 1, "↵ accept · ESC cancel", self.attr["dim"])
                self.scr.refresh()
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return options[sel][1]
                    if key in ("\x1b", "q"):
                        return None
                    if key == "j":
                        sel = min(len(options) - 1, sel + 1)
                    elif key == "k":
                        sel = max(0, sel - 1)
                    elif key.isdigit() and 0 < int(key) <= len(options):
                        return options[int(key) - 1][1]
                elif key == curses.KEY_DOWN:
                    sel = min(len(options) - 1, sel + 1)
                elif key == curses.KEY_UP:
                    sel = max(0, sel - 1)
        finally:
            self.scr.timeout(150)
            self.draw()

    def confirm(self, question: str) -> bool:
        h, w = self.scr.getmaxyx()
        lines = _wrap(question, w - 2) or [question]
        for i, line in enumerate(lines):
            y = h - len(lines) + i
            self._addstr(y, 0, " " * (w - 1))
            self._addstr(y, 0, f" {line}", self.attr["help"] | curses.A_BOLD)
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
    try:
        quit_all = curses.wrapper(lambda scr: Sidebar(scr, manager).run())
    except KeyboardInterrupt:
        # C-c in the slivers outside Sidebar.run's own handler (curses
        # setup/teardown): same graceful quit.
        quit_all = True
    if quit_all:
        # Tear the dashboard down AFTER curses restored the terminal.
        manager.shutdown_dashboard()

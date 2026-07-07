"""Thin, testable wrapper around the tmux CLI for our dedicated server.

Model: every Claude instance is a tmux *pane*, addressed by its immutable
pane id ("%N" — stable across moves between windows/sessions). Undisplayed
instances park as windows of the WORK_SESSION; the displayed one lives in
the dashboard window (DASH_SESSION:DASH_WINDOW) next to the sidebar pane,
and selecting another instance is a `swap-pane`. This is what lets the user
type into the real Claude TUI while the sidebar stays visible.

State-changing calls raise TmuxError on failure; queries return neutral
values (empty list / None) when the server isn't running yet, because "no
server" is a normal state before the first instance is created.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

from .config import Config, DASH_SESSION, DASH_WINDOW, SIDEBAR_WIDTH, WORK_SESSION


class TmuxError(RuntimeError):
    pass


@dataclass
class Pane:
    pane_id: str
    session: str
    window_id: str
    window_name: str
    dead: bool
    start_command: str


class Tmux:
    def __init__(self, config: Config):
        self.config = config

    # -- command plumbing ---------------------------------------------------

    def base_argv(self) -> list[str]:
        return ["tmux", "-L", self.config.socket_name, "-f", str(self.config.tmux_conf_path)]

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(self.base_argv() + list(args), capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise TmuxError(
                f"tmux {' '.join(shlex.quote(a) for a in args)} failed: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def write_conf(self) -> None:
        self.config.ensure_dirs()
        self.config.tmux_conf_path.write_text(self.config.render_tmux_conf())

    def source_conf(self) -> None:
        """Apply our conf to an ALREADY RUNNING server. tmux only reads -f at
        server start, and something else (a raw `tmux -L multi-claude ...`)
        may have started the server first — without remain-on-exit and our
        key bindings, the dashboard misbehaves subtly."""
        self._run("source-file", str(self.config.tmux_conf_path), check=False)

    # -- queries ------------------------------------------------------------

    def list_sessions(self) -> list[str]:
        proc = self._run("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:  # no server yet
            return []
        return [line for line in proc.stdout.splitlines() if line]

    def has_session(self, name: str) -> bool:
        return name in self.list_sessions()

    def list_panes(self) -> list[Pane]:
        """Every pane on the server, in one call."""
        proc = self._run(
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{session_name}\t#{window_id}\t#{window_name}\t"
            "#{pane_dead}\t#{pane_start_command}",
            check=False,
        )
        if proc.returncode != 0:
            return []
        panes = []
        for line in proc.stdout.splitlines():
            fields = (line.split("\t", 5) + [""] * 6)[:6]
            pane_id, session, window_id, window_name, dead, start = fields
            if pane_id:
                panes.append(Pane(pane_id, session, window_id, window_name, dead == "1", start))
        return panes

    def pane_exists(self, pane_id: str) -> bool:
        return any(p.pane_id == pane_id for p in self.list_panes())

    def capture_pane(self, pane_id: str, lines: int = 0) -> str:
        """Plain-text capture. lines <= 0 captures only the visible screen
        (what status heuristics must be based on — scrollback holds stale
        frames of the TUI)."""
        args = ["capture-pane", "-p", "-t", pane_id]
        if lines > 0:
            args += ["-S", f"-{lines}"]
        proc = self._run(*args, check=False)
        return proc.stdout if proc.returncode == 0 else ""

    def dash_panes(self) -> list[Pane]:
        return [p for p in self.list_panes() if p.session == DASH_SESSION]

    # -- instance panes -----------------------------------------------------

    def ensure_work_session(self) -> None:
        if not self.has_session(WORK_SESSION):
            # Keep-alive window so the session survives having zero instances
            # (and so displayed instances always have a home to swap back to).
            self._run(
                "new-session", "-d", "-s", WORK_SESSION, "-n", "-keep",
                "-x", "220", "-y", "50", "sleep infinity",
            )

    def spawn_instance(self, name: str, cwd: str, command: list[str]) -> str:
        """New instance pane in a WORK_SESSION window; returns its pane id."""
        self.write_conf()
        self.ensure_work_session()
        proc = self._run(
            "new-window", "-d", "-t", f"={WORK_SESSION}:", "-n", name, "-c", cwd,
            "-P", "-F", "#{pane_id}",
            shlex.join(command),
        )
        return proc.stdout.strip()

    def kill_pane(self, pane_id: str) -> None:
        self._run("kill-pane", "-t", pane_id, check=False)

    def respawn_pane(self, pane_id: str, cwd: str, command: list[str]) -> None:
        self._run("respawn-pane", "-k", "-t", pane_id, "-c", cwd, shlex.join(command))

    def respawn_shell(self, pane_id: str, shell_cmd: str) -> None:
        """Respawn a pane with a raw shell command (our own furniture)."""
        self._run("respawn-pane", "-k", "-t", pane_id, shell_cmd, check=False)

    def send_text(self, pane_id: str, text: str) -> None:
        """Type `text` into the pane followed by Enter. -l keeps it literal."""
        self._run("send-keys", "-t", pane_id, "-l", text)
        self._run("send-keys", "-t", pane_id, "Enter")

    def swap_panes(self, a: str, b: str) -> None:
        # -d keeps input focus where it is; callers set focus explicitly.
        self._run("swap-pane", "-d", "-s", a, "-t", b)

    def join_pane_right(self, src_pane: str, dst_pane: str) -> None:
        """Move src_pane into dst_pane's window as a new pane to its right
        (rebuilds the viewer slot when it was lost)."""
        self._run("join-pane", "-d", "-h", "-s", src_pane, "-t", dst_pane)

    def spawn_shell_window(self, name: str, shell_cmd: str) -> str:
        """New WORK_SESSION window running a raw shell command (our own
        furniture, e.g. a replacement welcome pane); returns its pane id."""
        self.ensure_work_session()
        proc = self._run(
            "new-window", "-d", "-t", f"={WORK_SESSION}:", "-n", name,
            "-P", "-F", "#{pane_id}", shell_cmd,
        )
        return proc.stdout.strip()

    def select_pane(self, pane_id: str) -> None:
        self._run("select-pane", "-t", pane_id, check=False)

    def rename_window_of_pane(self, pane_id: str, name: str) -> None:
        """Cosmetic: keep WORK_SESSION window names matching their instance."""
        proc = self._run("display-message", "-p", "-t", pane_id, "#{window_id}", check=False)
        window_id = proc.stdout.strip()
        if window_id:
            # "--" so names with a leading dash ("-welcome") aren't flags.
            self._run("rename-window", "-t", window_id, "--", name, check=False)

    def resize_pane_width(self, pane_id: str, width: int) -> None:
        self._run("resize-pane", "-t", pane_id, "-x", str(width), check=False)

    def unzoom_dash(self) -> None:
        """Clear zoom on the dashboard window before swapping panes
        (resize-pane -Z toggles, so only fire when actually zoomed)."""
        if self.dash_zoomed():
            self._run(
                "resize-pane", "-t", f"={DASH_SESSION}:{DASH_WINDOW}", "-Z", check=False
            )

    def dash_zoomed(self) -> bool:
        proc = self._run(
            "display-message", "-p", "-t", f"={DASH_SESSION}:{DASH_WINDOW}",
            "#{window_zoomed_flag}", check=False,
        )
        return proc.stdout.strip() == "1"

    # -- dashboard ----------------------------------------------------------

    def dashboard_exists(self) -> bool:
        return self.has_session(DASH_SESSION)

    def create_dashboard(self, sidebar_cmd: str, welcome_cmd: str) -> None:
        """Dashboard window: sidebar pane (left, fixed width) + viewer pane."""
        self.write_conf()
        self._run(
            "new-session", "-d", "-s", DASH_SESSION, "-n", DASH_WINDOW,
            "-x", "220", "-y", "50", sidebar_cmd,
        )
        self._run(
            "split-window", "-d", "-h", "-t", f"={DASH_SESSION}:{DASH_WINDOW}",
            welcome_cmd,
        )
        self._run(
            "resize-pane", "-t", f"={DASH_SESSION}:{DASH_WINDOW}.0",
            "-x", str(SIDEBAR_WIDTH), check=False,
        )

    def respawn_dead_dash_panes(self, sidebar_cmd: str, welcome_cmd: str) -> None:
        """If the sidebar or welcome process died (crash), bring it back."""
        for pane in self.dash_panes():
            if not pane.dead:
                continue
            cmd = sidebar_cmd if "sidebar" in pane.start_command else welcome_cmd
            self._run("respawn-pane", "-k", "-t", pane.pane_id, cmd, check=False)

    def attach_dashboard_argv(self) -> list[str]:
        return self.base_argv() + ["attach-session", "-t", f"={DASH_SESSION}"]

    def attach_env(self) -> dict[str, str]:
        """Drop $TMUX so tmux doesn't refuse to nest inside the user's own
        server."""
        env = dict(os.environ)
        env.pop("TMUX", None)
        return env

    def attach_dashboard(self) -> int:
        return subprocess.call(self.attach_dashboard_argv(), env=self.attach_env())

    def detach_dashboard_clients(self) -> None:
        self._run("detach-client", "-s", f"={DASH_SESSION}", check=False)

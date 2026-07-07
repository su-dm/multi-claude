"""Thin, testable wrapper around the tmux CLI for our dedicated server.

All state-changing calls raise TmuxError on failure; queries return neutral
values (empty list / None) when the server simply isn't running yet, because
"no server" is a normal state before the first instance is created.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

from .config import Config, TMUX_CONF


class TmuxError(RuntimeError):
    pass


@dataclass
class PaneInfo:
    session: str
    dead: bool
    width: int
    height: int


class Tmux:
    def __init__(self, config: Config):
        self.config = config

    # -- command plumbing ---------------------------------------------------

    def base_argv(self) -> list[str]:
        return ["tmux", "-L", self.config.socket_name, "-f", str(self.config.tmux_conf_path)]

    def _run(
        self, *args: str, check: bool = True, input_text: str | None = None
    ) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            self.base_argv() + list(args),
            capture_output=True,
            text=True,
            input=input_text,
        )
        if check and proc.returncode != 0:
            raise TmuxError(
                f"tmux {' '.join(shlex.quote(a) for a in args)} failed: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def write_conf(self) -> None:
        self.config.ensure_dirs()
        self.config.tmux_conf_path.write_text(TMUX_CONF)

    # -- queries ------------------------------------------------------------

    def server_running(self) -> bool:
        return self._run("has-session", check=False).returncode == 0

    def list_sessions(self) -> list[str]:
        proc = self._run("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:  # no server yet
            return []
        return [line for line in proc.stdout.splitlines() if line]

    def has_session(self, name: str) -> bool:
        # -t matching is prefix-based in some tmux versions; use exact match.
        return name in self.list_sessions()

    def pane_info(self, session: str) -> PaneInfo | None:
        proc = self._run(
            "list-panes",
            "-t",
            f"={session}:",
            "-F",
            "#{pane_dead}\t#{pane_width}\t#{pane_height}",
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        dead, width, height = proc.stdout.splitlines()[0].split("\t")
        return PaneInfo(session=session, dead=dead == "1", width=int(width), height=int(height))

    def capture_pane(self, session: str, lines: int = 0) -> str:
        """Plain-text capture of the session's pane.

        lines > 0 includes that much scrollback; lines <= 0 captures only the
        visible screen (what status heuristics must be based on — scrollback
        contains stale frames of the TUI).
        """
        args = ["capture-pane", "-p", "-t", f"={session}:"]
        if lines > 0:
            args += ["-S", f"-{lines}"]
        proc = self._run(*args, check=False)
        return proc.stdout if proc.returncode == 0 else ""

    # -- mutations ----------------------------------------------------------

    def new_session(self, name: str, cwd: str, command: list[str]) -> None:
        self.write_conf()
        self._run(
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            cwd,
            "-x",
            "220",
            "-y",
            "50",
            shlex.join(command),
        )

    def kill_session(self, name: str) -> None:
        self._run("kill-session", "-t", f"={name}", check=False)

    def respawn(self, session: str, cwd: str, command: list[str]) -> None:
        self._run(
            "respawn-pane", "-k", "-t", f"={session}:", "-c", cwd, shlex.join(command)
        )

    def rename_session(self, old: str, new: str) -> None:
        self._run("rename-session", "-t", f"={old}", new)

    def send_text(self, session: str, text: str) -> None:
        """Type `text` into the session followed by Enter.

        send-keys -l keeps the text literal (no key-name interpretation);
        Enter is sent separately after a brief flush so TUI apps that debounce
        paste-like input still submit.
        """
        self._run("send-keys", "-t", f"={session}:", "-l", text)
        self._run("send-keys", "-t", f"={session}:", "Enter")

    # -- attach -------------------------------------------------------------

    def attach_argv(self, session: str) -> list[str]:
        return self.base_argv() + ["attach-session", "-t", f"={session}"]

    def attach_env(self) -> dict[str, str]:
        """Environment for a (possibly nested) attach: drop $TMUX so tmux
        doesn't refuse to nest inside the user's own server."""
        env = dict(os.environ)
        env.pop("TMUX", None)
        return env

    def attach(self, session: str) -> int:
        """Attach to a session, blocking until the user detaches (C-q)."""
        return subprocess.call(self.attach_argv(session), env=self.attach_env())

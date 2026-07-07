"""Configuration and filesystem paths.

Everything is overridable via environment variables so tests can point the
tool at a throwaway socket/state directory without touching the real one.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "MULTI_CLAUDE_"

# tmux session layout on our dedicated server:
DASH_SESSION = "mc-dash"     # the dashboard: sidebar pane + viewer pane
DASH_WINDOW = "dash"
WORK_SESSION = "mc-work"     # parking lot: one window per undisplayed instance
SIDEBAR_WIDTH = 34           # default; runtime-adjustable with < / > (persisted)


def _env(name: str, default: str) -> str:
    # An empty value (e.g. an unset shell variable interpolated into env)
    # falls back to the default rather than producing broken settings.
    return os.environ.get(ENV_PREFIX + name) or default


@dataclass
class Config:
    # tmux socket name: isolates our server from the user's tmux entirely.
    socket_name: str = field(default_factory=lambda: _env("SOCKET", "multi-claude"))
    # Where instance metadata + generated tmux.conf live.
    data_dir: Path = field(
        default_factory=lambda: Path(
            _env(
                "DATA_DIR",
                os.path.join(
                    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                    "multi-claude",
                ),
            )
        )
    )
    # Command used to launch an instance. Resolved to an absolute path at
    # spawn time because tmux's default shell may not share our PATH (nvm).
    claude_cmd: str = field(default_factory=lambda: _env("CLAUDE_CMD", "claude"))
    # Dashboard poll interval (seconds) for status refresh.
    poll_interval: float = field(
        default_factory=lambda: float(_env("POLL_INTERVAL", "1.0"))
    )
    # Ring a bell / notify-send when an instance starts waiting on you.
    notify: bool = field(default_factory=lambda: _env("NOTIFY", "1") != "0")
    # Claude Code's own data dir (transcripts live under <here>/projects/);
    # overridable so tests can fabricate transcripts.
    claude_home: Path = field(
        default_factory=lambda: Path(_env("CLAUDE_HOME", os.path.expanduser("~/.claude")))
    )
    # Sidebar pane width (columns); < / > adjust it at runtime and persist
    # the choice in settings.json.
    sidebar_width: int = field(
        default_factory=lambda: int(_env("SIDEBAR_WIDTH", str(SIDEBAR_WIDTH)))
    )

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "instances.json"

    @property
    def tmux_conf_path(self) -> Path:
        return self.data_dir / "tmux.conf"

    @property
    def costs_dir(self) -> Path:
        """Per-session cost JSONs captured from Claude Code's statusline
        hook (exact, CC-computed) — preferred over our pricing estimate."""
        return self.data_dir / "costs"

    def resolve_claude_cmd(self) -> str | None:
        """Absolute path to the claude binary, or None if not found."""
        return shutil.which(os.path.expanduser(self.claude_cmd))

    def claude_code_series(self) -> str:
        """major.minor of the installed Claude Code ("2.1"), or "" if
        unavailable. Cached — invoked once per process."""
        if not hasattr(self, "_cc_series"):
            series = ""
            binary = self.resolve_claude_cmd()
            if binary:
                try:
                    out = subprocess.run(
                        [binary, "--version"], capture_output=True, text=True, timeout=10
                    ).stdout
                    m = re.match(r"(\d+\.\d+)", out.strip())
                    series = m.group(1) if m else ""
                except (OSError, subprocess.TimeoutExpired):
                    pass
            object.__setattr__(self, "_cc_series", series)
        return self._cc_series

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- persisted user settings (data_dir/settings.json) --------------------

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def apply_saved_settings(self) -> None:
        """Load runtime-toggled settings (mtime-cached: cheap enough to call
        from the poll loop, so CLI toggles reach a running sidebar). An
        explicitly set env var wins (MULTI_CLAUDE_NOTIFY=0 always works)."""
        try:
            mtime = os.stat(self.settings_path).st_mtime_ns
        except OSError:
            return
        if getattr(self, "_settings_mtime", None) == mtime:
            return
        object.__setattr__(self, "_settings_mtime", mtime)
        try:
            data = json.loads(self.settings_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if "MULTI_CLAUDE_NOTIFY" not in os.environ and "notify" in data:
            self.notify = bool(data["notify"])
        if "MULTI_CLAUDE_SIDEBAR_WIDTH" not in os.environ and "sidebar_width" in data:
            try:
                self.sidebar_width = max(20, min(100, int(data["sidebar_width"])))
            except (TypeError, ValueError):
                pass

    def save_setting(self, key: str, value) -> None:
        self.ensure_dirs()
        try:
            data = json.loads(self.settings_path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        data[key] = value
        tmp = self.settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.settings_path)

    def mc_command(self) -> str:
        """Shell command that re-enters this CLI from tmux run-shell bindings.

        Must work from the tmux server's environment (which has neither our
        PYTHONPATH nor necessarily our PATH), so pin the interpreter, the
        package location, and the env vars that select socket/data dir.
        """
        repo = Path(__file__).resolve().parent.parent
        pinned = {
            f"{ENV_PREFIX}SOCKET": self.socket_name,
            f"{ENV_PREFIX}DATA_DIR": str(self.data_dir),
            f"{ENV_PREFIX}CLAUDE_CMD": self.claude_cmd,
            f"{ENV_PREFIX}CLAUDE_HOME": str(self.claude_home),
            f"{ENV_PREFIX}POLL_INTERVAL": str(self.poll_interval),
            # NOTIFY is deliberately NOT pinned: it's runtime-toggled (N key /
            # `notify on|off`) and persisted in data_dir/settings.json; an
            # env pin here would freeze it for the sidebar process.
        }
        parts = (
            ["env", f"PYTHONPATH={shlex.quote(str(repo))}"]
            + [f"{k}={shlex.quote(v)}" for k, v in pinned.items()]
            + [shlex.quote(sys.executable), "-m", "multi_claude"]
        )
        return " ".join(parts)

    def render_tmux_conf(self) -> str:
        mc = self.mc_command()
        select_binds = "\n".join(
            f'bind-key -n M-{n} run-shell "{mc} select {n}"' for n in range(1, 10)
        )
        return TMUX_CONF_TEMPLATE.format(
            mc=mc, select_binds=select_binds, option_binds=self._option_binds(mc)
        )

    def _option_binds(self, mc: str) -> str:
        """macOS fallbacks: terminals there don't send Alt/Meta by default —
        Option+h types "˙" etc. Bind those literal characters (US layout) so
        the navigation keys work without reconfiguring the terminal. Letters
        only: Option+digits produce £/§/…, which are real typeable symbols on
        common layouts and must keep reaching the agent panes.
        Opt out with MULTI_CLAUDE_OPTION_KEYS=0 (e.g. non-US layouts where
        one of these characters is a regular letter, like å)."""
        if sys.platform != "darwin" or _env("OPTION_KEYS", "1") == "0":
            return ""
        return "\n".join(
            [
                "# macOS: what Option+h/l/z/o/a type on a US layout (see above).",
                "bind-key -n ˙ select-pane -L",
                "bind-key -n ¬ select-pane -R",
                "bind-key -n Ω resize-pane -Z",
                f'bind-key -n ø run-shell "{mc} select next"',
                f'bind-key -n å run-shell "{mc} select attention"',
            ]
        )


# Config for OUR tmux server only; never touches the user's tmux setup.
# All bindings are prefix-less so they work when nested inside the user's
# own tmux session (Alt keys pass straight through an outer tmux).
TMUX_CONF_TEMPLATE = """\
# Generated by multi-claude; edits are overwritten at dashboard startup.

# Keep panes around when claude exits so we can show EXITED + last output
# and offer a restart (respawn-pane).
set -g remain-on-exit on

set -g mouse on
set -g history-limit 50000
set -g default-terminal "tmux-256color"
set -g escape-time 10

# -- dashboard keys (no prefix needed) --------------------------------------
# C-q: detach the dashboard client (everything keeps running).
bind-key -n C-q detach-client
# C-c on a dead pane (exited agent) quits the dashboard gracefully — same as
# C-c in the sidebar — instead of being swallowed by the dead pane. Live
# panes receive C-c unchanged (it interrupts claude / quits the sidebar).
bind-key -n C-c if-shell -F "#{{pane_dead}}" 'run-shell "{mc} quit"' 'send-keys C-c'
# Alt-h / Alt-l: move focus between the sidebar and the Claude pane.
bind-key -n M-h select-pane -L
bind-key -n M-l select-pane -R
# Alt-z: zoom the focused pane to full screen (again to restore).
bind-key -n M-z resize-pane -Z
# Alt-1..9: display instance N; Alt-o: cycle to the next instance.
{select_binds}
bind-key -n M-o run-shell "{mc} select next"
# Alt-a: jump to the next agent that needs your input.
bind-key -n M-a run-shell "{mc} select attention"
{option_binds}

set -g status on
set -g status-style "bg=colour236,fg=colour250"
set -g status-left "#[bold] multi-claude "
set -g status-left-length 20
set -g status-right " M-h/M-l panes · M-1..9/M-o switch · M-z zoom · C-q detach "
set -g status-right-length 70
"""

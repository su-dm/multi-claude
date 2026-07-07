"""InstanceManager: the model layer tying registry + tmux + status together.

The curses UI only talks to this class. A background poller thread refreshes
per-instance snapshots (status + preview text); the UI reads a consistent
copy under a lock. All tmux subprocess work happens here, never in the UI
event loop, so the interface stays responsive.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

from .config import Config
from .registry import Instance, Registry
from .status import Status, StatusInfo, classify
from .tmux import Tmux, TmuxError


@dataclass
class Snapshot:
    instance: Instance
    status: StatusInfo = field(default_factory=lambda: StatusInfo(Status.UNKNOWN))
    preview: str = ""
    session_alive: bool = False


class InstanceManager:
    def __init__(self, config: Config):
        self.config = config
        self.tmux = Tmux(config)
        config.ensure_dirs()
        self.registry = Registry(config.registry_path)
        self._lock = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._prev_status: dict[str, Status] = {}
        # Set by the poller when an instance transitions into a state that
        # wants attention; the UI consumes it to ring the bell / notify.
        self.attention_events: list[tuple[str, Status]] = []
        self.registry.adopt_unknown_sessions(self.tmux.list_sessions())

    # -- lifecycle ----------------------------------------------------------

    def create(self, cwd: str, name: str | None = None, claude_args: list[str] | None = None) -> Instance:
        cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"not a directory: {cwd}")
        claude = self.config.resolve_claude_cmd()
        if claude is None:
            raise ValueError(
                f"claude binary not found (looked for {self.config.claude_cmd!r}); "
                f"set {'MULTI_CLAUDE_CLAUDE_CMD'} or install Claude Code"
            )
        command = [claude] + list(claude_args or [])
        name = self.registry.unique_name(name or os.path.basename(cwd) or "claude")
        instance = Instance(name=name, cwd=cwd, command=command)
        self.tmux.new_session(name, cwd, command)
        self.registry.add(instance)
        self.poll_once()
        return instance

    def kill(self, name: str) -> None:
        self.tmux.kill_session(name)
        self.registry.remove(name)
        with self._lock:
            self._snapshots.pop(name, None)
        self._prev_status.pop(name, None)

    def restart(self, name: str) -> None:
        """Re-launch claude in an exited instance (dead pane or gone session)."""
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        command = inst.command or [self.config.resolve_claude_cmd() or "claude"]
        if self.tmux.has_session(name):
            self.tmux.respawn(name, inst.cwd, command)
        else:
            self.tmux.new_session(name, inst.cwd, command)
        self.poll_once()

    def rename(self, old: str, new: str) -> str:
        new = self.registry.unique_name(new)
        if self.tmux.has_session(old):
            self.tmux.rename_session(old, new)
        self.registry.rename(old, new)
        with self._lock:
            snap = self._snapshots.pop(old, None)
            if snap:
                self._snapshots[new] = snap
        self._prev_status.pop(old, None)
        return new

    def send_text(self, name: str, text: str) -> None:
        if not self.tmux.has_session(name):
            raise TmuxError(f"session {name!r} is not running")
        self.tmux.send_text(name, text)

    def attach(self, name: str) -> int:
        return self.tmux.attach(name)

    # -- polling ------------------------------------------------------------

    def poll_once(self) -> None:
        live = set(self.tmux.list_sessions())
        self.registry.adopt_unknown_sessions(sorted(live))
        events: list[tuple[str, Status]] = []
        snapshots: dict[str, Snapshot] = {}
        for inst in self.registry.instances:
            snap = Snapshot(instance=inst)
            if inst.name in live:
                snap.session_alive = True
                pane = self.tmux.pane_info(inst.name)
                dead = pane.dead if pane else True
                text = self.tmux.capture_pane(inst.name, lines=0)
                snap.preview = text
                snap.status = classify(text, pane_dead=dead)
            else:
                snap.status = StatusInfo(Status.EXITED)
            prev = self._prev_status.get(inst.name)
            now = snap.status.status
            if (
                prev in (Status.BUSY, Status.STARTING)
                and now.wants_attention
            ):
                events.append((inst.name, now))
            self._prev_status[inst.name] = now
            snapshots[inst.name] = snap
        with self._lock:
            self._snapshots = snapshots
            self.attention_events.extend(events)

    def snapshots(self) -> list[Snapshot]:
        """Ordered snapshots (registry order); safe copy for the UI thread."""
        with self._lock:
            by_name = dict(self._snapshots)
        return [by_name.get(i.name, Snapshot(instance=i)) for i in self.registry.instances]

    def drain_attention_events(self) -> list[tuple[str, Status]]:
        with self._lock:
            events, self.attention_events = self.attention_events, []
        return events

    def start_polling(self) -> None:
        if self._poll_thread:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    # Polling must never take the dashboard down; transient
                    # tmux races (session killed mid-poll) are expected.
                    pass
                self._stop.wait(self.config.poll_interval)

        self._poll_thread = threading.Thread(target=loop, name="mc-poller", daemon=True)
        self._poll_thread.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=2)
            self._poll_thread = None

    # -- notifications --------------------------------------------------------

    def notify(self, name: str, status: Status) -> None:
        """Best-effort desktop notification; the UI also rings the bell."""
        if not self.config.notify:
            return
        if shutil.which("notify-send"):
            subprocess.Popen(
                ["notify-send", "-a", "multi-claude", f"{name}: {status.value}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

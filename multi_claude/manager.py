"""InstanceManager: the model layer tying registry + tmux + status together.

The sidebar UI and the CLI both talk only to this class. A background poller
thread refreshes per-instance snapshots (status via marker matching plus
screen-change detection); the UI reads a consistent copy under a lock.

Display model: the dashboard window holds the sidebar pane and one "viewer
slot". `display()` swaps the chosen instance's pane into that slot (the
previous occupant swaps back out to its parking window in WORK_SESSION), so
the user interacts with the real Claude pane directly.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from .config import Config, DASH_SESSION, WORK_SESSION
from .registry import Instance, Registry
from .status import Status, StatusInfo, classify
from .tmux import Pane, Tmux, TmuxError
from .gitinfo import (
    GitError,
    GitStatus,
    GitStatusCache,
    create_worktree,
    is_git_repo,
    repo_identity,
)
from .statusline import reported_cost
from .transcripts import SessionInfo, TokenReader


@dataclass
class Snapshot:
    instance: Instance
    status: StatusInfo = field(default_factory=lambda: StatusInfo(Status.EXITED))
    pane_alive: bool = False  # pane exists (possibly dead process)
    displayed: bool = False
    tokens: int | None = None  # context tokens of the agent's session
    session: SessionInfo | None = None  # model/cost/activity from transcript
    git: GitStatus | None = None


def _is_instance_pane(pane: Pane) -> bool:
    """Instance panes vs. our own furniture (sidebar/welcome/keep-alive)."""
    if pane.session not in (WORK_SESSION, DASH_SESSION):
        return False
    if pane.window_name == "-keep":
        return False
    # tmux quotes start commands it received as a single argument.
    cmd = pane.start_command.strip('"')
    if "-m multi_claude" in cmd or cmd.startswith("sleep"):
        return False
    return True


class InstanceManager:
    def __init__(self, config: Config):
        self.config = config
        self.tmux = Tmux(config)
        config.ensure_dirs()
        config.apply_saved_settings()
        self.registry = Registry(config.registry_path)
        self._lock = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._prev_status: dict[str, Status] = {}
        self._prev_screen: dict[str, int] = {}  # pane_id -> hash of last capture
        self._tokens = TokenReader(config.claude_home)
        self._git = GitStatusCache()
        self.attention_events: list[tuple[str, Status]] = []

    # -- dashboard plumbing ---------------------------------------------------

    def sidebar_cmd(self) -> str:
        return f"{self.config.mc_command()} sidebar"

    def welcome_cmd(self) -> str:
        return f"{self.config.mc_command()} welcome"

    def bootstrap_dashboard(self) -> None:
        """Create (or repair) the dashboard session; adopt/migrate strays."""
        self.tmux.write_conf()
        if self.tmux.list_sessions():
            # Server already running — it may have started without our conf
            # (tmux reads -f only at server start), so apply it now.
            self.tmux.source_conf()
        self.migrate_legacy_sessions()
        if not self.tmux.dashboard_exists():
            self.tmux.create_dashboard(self.sidebar_cmd(), self.welcome_cmd())
        else:
            self.tmux.respawn_dead_dash_panes(self.sidebar_cmd(), self.welcome_cmd())
            self.ensure_viewer()
        self.poll_once()

    def refresh_ui(self) -> None:
        """Restart the sidebar/welcome processes in place (e.g. after
        upgrading multi-claude) WITHOUT touching instance panes. Never kill
        the mc-dash session for this — a displayed instance lives inside it
        and would die with it."""
        if not self.tmux.dashboard_exists():
            self.bootstrap_dashboard()
            return
        self.tmux.write_conf()
        self.tmux.source_conf()
        displayed = self.registry.get_by_pane(self.displayed_pane_id())
        if displayed is not None:
            self.display_welcome()  # park it clear of the respawns
        sidebar = self._sidebar_pane()
        if sidebar is not None:
            self.tmux.respawn_shell(sidebar.pane_id, self.sidebar_cmd())
        welcome = self._welcome_pane()
        if welcome is not None:
            self.tmux.respawn_shell(welcome.pane_id, self.welcome_cmd())
        self.ensure_viewer()
        if displayed is not None:
            self.display(displayed.name, focus=False)
        self.poll_once()

    def migrate_legacy_sessions(self) -> None:
        """v0.1 ran each instance as its own tmux session; fold those (and any
        stray sessions someone created on our socket) into WORK_SESSION."""
        strays = [
            s for s in self.tmux.list_sessions() if s not in (DASH_SESSION, WORK_SESSION)
        ]
        if not strays:
            return
        self.tmux.ensure_work_session()
        for sess in strays:
            # Pane ids are immutable across moves — grab them BEFORE moving.
            # (The window name inside a v0.1 session is "claude", not the
            # session name, so it can't be used to find the pane afterwards.)
            sess_panes = [p for p in self.tmux.list_panes() if p.session == sess]
            try:
                self.tmux._run("move-window", "-d", "-s", f"={sess}:", "-t", f"={WORK_SESSION}:")
            except TmuxError:
                continue
            if not sess_panes:
                continue
            pane_id = sess_panes[0].pane_id
            # Re-point the registry entry (v0.1 keyed instances by session name).
            inst = self.registry.get(sess)
            if inst and not inst.pane_id:
                inst.pane_id = pane_id
                self.registry.save()
            self.tmux.rename_window_of_pane(pane_id, sess)

    # -- viewer slot ----------------------------------------------------------

    def _sidebar_pane(self) -> Pane | None:
        for pane in self.tmux.dash_panes():
            if "multi_claude sidebar" in pane.start_command:
                return pane
        return None

    def viewer_pane(self) -> Pane | None:
        """The dashboard pane next to the sidebar (instance or welcome)."""
        sidebar = self._sidebar_pane()
        for pane in self.tmux.dash_panes():
            if sidebar is None or pane.pane_id != sidebar.pane_id:
                return pane
        return None

    def displayed_pane_id(self) -> str:
        viewer = self.viewer_pane()
        return viewer.pane_id if viewer else ""

    def _welcome_pane(self) -> Pane | None:
        """The welcome placeholder pane. Only one may exist: duplicates can
        appear if ensure_viewer spawned a spare while another welcome was
        parked — prefer the one in the dashboard, kill the rest."""
        found = [
            p for p in self.tmux.list_panes()
            if "multi_claude welcome" in p.start_command
        ]
        if not found:
            return None
        found.sort(key=lambda p: p.session != DASH_SESSION)  # dash first
        for extra in found[1:]:
            self.tmux.kill_pane(extra.pane_id)
        return found[0]

    def display(self, name: str, focus: bool = True) -> None:
        """Swap the instance's pane into the viewer slot."""
        self.registry.maybe_reload()
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        if not inst.pane_id or not self.tmux.pane_exists(inst.pane_id):
            raise TmuxError(f"{name} has no pane (R restarts it)")
        viewer = self.viewer_pane()
        if viewer is None:
            self.ensure_viewer()
            viewer = self.viewer_pane()
        if viewer is None:
            raise TmuxError("dashboard is not running (run: multi-claude)")
        self.tmux.unzoom_dash()
        if viewer.pane_id != inst.pane_id:
            self.tmux.swap_panes(inst.pane_id, viewer.pane_id)
            # Cosmetic: the parking window that now holds the old viewer pane
            # should carry that pane's identity.
            old = self.registry.get_by_pane(viewer.pane_id)
            self.tmux.rename_window_of_pane(
                viewer.pane_id, old.name if old else "-welcome"
            )
        if focus:
            self.tmux.select_pane(inst.pane_id)

    def ensure_viewer(self) -> None:
        """Self-heal a dashboard whose viewer slot disappeared (e.g. the
        displayed pane was destroyed while remain-on-exit wasn't active):
        join the welcome pane back in next to the sidebar."""
        if not self.tmux.dashboard_exists() or self.viewer_pane() is not None:
            return
        sidebar = self._sidebar_pane()
        if sidebar is None:
            return
        welcome = self._welcome_pane()
        welcome_id = welcome.pane_id if welcome else self.tmux.spawn_shell_window(
            "-welcome", self.welcome_cmd()
        )
        self.tmux.join_pane_right(welcome_id, sidebar.pane_id)
        self.tmux.resize_pane_width(sidebar.pane_id, self.config.sidebar_width)

    def display_welcome(self) -> None:
        """Put the welcome pane (back) into the viewer slot."""
        viewer = self.viewer_pane()
        welcome = self._welcome_pane()
        if viewer is None:
            return
        if welcome is None:
            # Welcome process was killed somehow; respawn it in place.
            self.tmux.respawn_shell(viewer.pane_id, self.welcome_cmd())
            return
        if viewer.pane_id != welcome.pane_id:
            self.tmux.unzoom_dash()
            self.tmux.swap_panes(welcome.pane_id, viewer.pane_id)
            old = self.registry.get_by_pane(viewer.pane_id)
            if old:
                self.tmux.rename_window_of_pane(viewer.pane_id, old.name)

    def shutdown_dashboard(self) -> None:
        """Graceful quit (C-c in the sidebar/welcome pane): park the displayed
        instance back to its work window so it survives, then kill the
        dashboard session. Instances keep running in WORK_SESSION;
        `multi-claude` reopens the dashboard.

        Runs from a process whose pane lives inside mc-dash, so ignore the
        SIGHUP that killing the session sends us — we want to finish cleanly,
        not die mid-teardown."""
        self.stop_polling()
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except (OSError, ValueError):
            pass  # not the main thread / unsupported — teardown still works
        try:
            self.display_welcome()
        except TmuxError:
            pass
        self.tmux.kill_dash_session()

    def select(self, which: str) -> None:
        """CLI hook for tmux bindings: `select 3`, `select next`, `select NAME`.

        Numeric selection uses the same ordering the sidebar shows (registry
        order, all instances); next/prev cycle over instances whose pane
        still exists.
        """
        snaps = self.snapshots_fresh()
        if not snaps:
            return
        if which == "attention":
            # Jump to the next agent that is blocked on the user, scanning
            # forward from the currently displayed one.
            alive = [s for s in snaps if s.pane_alive]
            if not alive:
                return
            current = next((i for i, s in enumerate(alive) if s.displayed), -1)
            rotated = alive[current + 1 :] + alive[: current + 1]
            target = next(
                (s for s in rotated if s.status.status is Status.HELP), None
            )
            if target is None:
                return  # nobody needs help; quiet no-op from a key binding
        elif which in ("next", "prev"):
            alive = [s for s in snaps if s.pane_alive]
            if not alive:
                return
            step = 1 if which == "next" else -1
            current = next((i for i, s in enumerate(alive) if s.displayed), -step)
            target = alive[(current + step) % len(alive)]
        elif which.isdigit():
            idx = int(which) - 1
            if not (0 <= idx < len(snaps)) or not snaps[idx].pane_alive:
                return  # quiet no-op from a key binding
            target = snaps[idx]
        else:
            target = next((s for s in snaps if s.instance.name == which), None)
            if target is None:
                raise KeyError(which)
        self.display(target.instance.name)

    # -- instance lifecycle -----------------------------------------------------

    def is_git_dir(self, cwd: str) -> bool:
        return is_git_repo(os.path.abspath(os.path.expanduser(cwd)))

    def repo_top(self, cwd: str) -> str | None:
        return repo_identity(os.path.abspath(os.path.expanduser(cwd)))

    def create(
        self,
        cwd: str,
        name: str | None = None,
        claude_args: list[str] | None = None,
        worktree_branch: str | None = None,
    ) -> Instance:
        """Spawn an instance in cwd. worktree_branch isolates the agent on a
        fresh git worktree of the repo at cwd (created under
        <repo>.worktrees/<branch>) instead of the shared checkout."""
        self.registry.maybe_reload()
        cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"not a directory: {cwd}")
        if worktree_branch:
            try:
                cwd = create_worktree(cwd, worktree_branch.strip())
            except GitError as exc:
                raise ValueError(str(exc)) from exc
        claude = self.config.resolve_claude_cmd()
        if claude is None:
            raise ValueError(
                f"claude binary not found (looked for {self.config.claude_cmd!r}); "
                "set MULTI_CLAUDE_CLAUDE_CMD or install Claude Code"
            )
        command = [claude] + list(claude_args or [])
        name = self.registry.unique_name(name or os.path.basename(cwd) or "claude")
        pane_id = self.tmux.spawn_instance(name, cwd, command)
        self.registry.add(Instance(name=name, cwd=cwd, command=command, pane_id=pane_id))
        self.poll_once()
        return self.registry.get(name)  # type: ignore[return-value]

    def kill(self, name: str) -> None:
        self.registry.maybe_reload()
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        if inst.pane_id:
            if self.displayed_pane_id() == inst.pane_id:
                self.display_welcome()  # never leave the viewer slot empty
            self.tmux.kill_pane(inst.pane_id)
        self.registry.remove(name)
        self.ensure_viewer()
        with self._lock:
            self._snapshots.pop(name, None)
        self._prev_status.pop(name, None)
        self._prev_screen.pop(inst.pane_id, None)
        self._tokens.forget(name)

    def archive(self, name: str) -> None:
        """Kill the agent's pane but keep it in the registry (hidden from the
        sidebar). Its conversation stays resumable via unarchive."""
        self.registry.maybe_reload()
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        if inst.pane_id and self.tmux.pane_exists(inst.pane_id):
            if self.displayed_pane_id() == inst.pane_id:
                self.display_welcome()
            self.tmux.kill_pane(inst.pane_id)
        inst.pane_id = ""
        inst.archived = True
        self.registry.save()
        self._tokens.forget(name)
        self.poll_once()

    def unarchive(self, name: str, resume: bool = True) -> None:
        """Bring an archived agent back: relaunch it (continuing its old
        conversation by default) and show it in the sidebar again."""
        self.registry.maybe_reload()
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        inst.archived = False
        self.registry.save()
        self.restart(name, resume=resume)

    def toggle_notify(self) -> bool:
        self.config.notify = not self.config.notify
        self.config.save_setting("notify", self.config.notify)
        return self.config.notify

    def toggle_pin(self, name: str) -> bool:
        self.registry.maybe_reload()
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        inst.pinned = not inst.pinned
        self.registry.save()
        self.poll_once()
        return inst.pinned

    def restart(self, name: str, resume: bool = False) -> None:
        """Re-launch claude for an exited instance (dead pane or gone pane).

        resume=True continues the previous conversation: precisely via
        `--resume <session-id>` when the poller learned the session id,
        otherwise `--continue` (most recent conversation in that cwd).
        """
        self.registry.maybe_reload()
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        command = list(inst.command) or [self.config.resolve_claude_cmd() or "claude"]
        if resume:
            command = [
                a for a in command
                if a not in ("--continue", "-c", "--resume", "-r") and a != inst.session_id
            ]
            command += ["--resume", inst.session_id] if inst.session_id else ["--continue"]
        if inst.pane_id and self.tmux.pane_exists(inst.pane_id):
            self.tmux.respawn_pane(inst.pane_id, inst.cwd, command)
        else:
            inst.pane_id = self.tmux.spawn_instance(inst.name, inst.cwd, command)
        inst.started_at = time.time()  # a fresh claude session begins now
        self.registry.save()
        self._tokens.forget(inst.name)
        self.poll_once()

    def rename(self, old: str, new: str) -> str:
        self.registry.maybe_reload()
        new = self.registry.unique_name(new)
        inst = self.registry.get(old)
        if inst is None:
            raise KeyError(old)
        self.registry.rename(old, new)
        if inst.pane_id and self.tmux.pane_exists(inst.pane_id):
            self.tmux.rename_window_of_pane(inst.pane_id, new)
        with self._lock:
            snap = self._snapshots.pop(old, None)
            if snap:
                self._snapshots[new] = snap
        self._prev_status.pop(old, None)
        return new

    def send_text(self, name: str, text: str) -> None:
        self.registry.maybe_reload()
        inst = self.registry.get(name)
        if inst is None:
            raise KeyError(name)
        if not inst.pane_id or not self.tmux.pane_exists(inst.pane_id):
            raise TmuxError(f"{name} is not running")
        self.tmux.send_text(inst.pane_id, text)

    # -- polling ------------------------------------------------------------

    def poll_once(self) -> None:
        self.config.apply_saved_settings()
        self.registry.maybe_reload()
        panes = {p.pane_id: p for p in self.tmux.list_panes()}
        self._adopt_strays(panes)
        displayed = self.displayed_pane_id()
        events: list[tuple[str, Status]] = []
        snapshots: dict[str, Snapshot] = {}
        for inst in self.registry.instances:
            snap = Snapshot(instance=inst)
            pane = panes.get(inst.pane_id)
            if pane is not None:
                snap.pane_alive = True
                snap.displayed = inst.pane_id == displayed
                text = self.tmux.capture_pane(inst.pane_id)
                digest = hash(text)
                changed = (
                    inst.pane_id in self._prev_screen
                    and self._prev_screen[inst.pane_id] != digest
                )
                self._prev_screen[inst.pane_id] = digest
                snap.status = classify(text, pane_dead=pane.dead, changed=changed)
                snap.session = self._tokens.info_for(
                    inst.name, inst.cwd, inst.started_at or inst.created_at
                )
                if snap.session:
                    snap.tokens = snap.session.tokens
                    exact = reported_cost(self.config, snap.session.session_id)
                    if exact is not None:
                        # Claude Code's own number (statusline hook) beats
                        # our pricing-table estimate.
                        snap.session.cost_usd = exact
                        snap.session.cost_source = "claude"
                    if snap.session.session_id and inst.session_id != snap.session.session_id:
                        # Remember the session so we can --resume it after a
                        # reboot/crash. Saved once per change, not per poll.
                        inst.session_id = snap.session.session_id
                        self.registry.save()
                snap.git = self._git.status(inst.cwd)
            else:
                snap.status = StatusInfo(Status.EXITED)
            prev = self._prev_status.get(inst.name)
            now = snap.status.status
            if (
                prev in (Status.WORKING, Status.STARTING)
                and now.wants_attention
                and not inst.archived
            ):
                events.append((inst.name, now))
            self._prev_status[inst.name] = now
            snapshots[inst.name] = snap
        with self._lock:
            self._snapshots = snapshots
            self.attention_events.extend(events)

    def _adopt_strays(self, panes: dict[str, Pane]) -> None:
        """Instance panes on the server that the registry doesn't know
        (state file lost) get registered rather than orphaned."""
        strays = [
            (p.pane_id, p.window_name)
            for p in panes.values()
            if _is_instance_pane(p) and self.registry.get_by_pane(p.pane_id) is None
        ]
        if strays:
            self.registry.adopt_panes(strays)

    def snapshots(self, include_archived: bool = False) -> list[Snapshot]:
        """Snapshots in sidebar order (pinned first, archived hidden unless
        requested); safe copy for the UI thread."""
        with self._lock:
            by_name = dict(self._snapshots)
        return [
            by_name.get(i.name, Snapshot(instance=i))
            for i in self.registry.ordered(include_archived)
        ]

    def snapshots_fresh(self) -> list[Snapshot]:
        self.poll_once()
        return self.snapshots()

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
                    # tmux races (pane killed mid-poll) are expected.
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
        if sys.platform == "darwin":
            # json.dumps produces a double-quoted, backslash-escaped literal,
            # which is also valid AppleScript string syntax.
            script = (
                f"display notification {json.dumps(f'{name}: {status.value}')}"
                ' with title "multi-claude"'
            )
            cmd = ["osascript", "-e", script]
        elif shutil.which("notify-send"):
            cmd = ["notify-send", "-a", "multi-claude", f"{name}: {status.value}"]
        else:
            return
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

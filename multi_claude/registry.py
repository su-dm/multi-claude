"""Instance metadata registry, persisted as JSON.

tmux is the source of truth for *liveness*; the registry adds metadata that
tmux can't hold (working directory as requested, creation time, the command
used, so exited instances can be restarted) and lets the dashboard remember
instances whose sessions have terminated entirely.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Instance:
    name: str  # doubles as the tmux window name (cosmetic; panes are truth)
    cwd: str
    command: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Immutable tmux pane id ("%N") — the instance's real handle. Empty for
    # entries whose pane is gone entirely (restartable from metadata).
    pane_id: str = ""
    # When the current claude process started (updated on restart); used to
    # match the instance to its Claude Code session transcript.
    started_at: float = 0.0
    # Claude Code session id (transcript filename stem), learned by the
    # poller once the transcript is matched. Enables precise resume
    # (claude --resume <id>) after a reboot or crash.
    session_id: str = ""
    # Pinned instances sort to the top of the sidebar.
    pinned: bool = False
    # Archived: hidden from the sidebar, pane killed, but metadata +
    # session_id kept so the conversation can be revived later.
    archived: bool = False

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.created_at

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Instance":
        return cls(
            name=data["name"],
            cwd=data["cwd"],
            command=list(data.get("command", [])),
            created_at=float(data.get("created_at", 0)),
            pane_id=data.get("pane_id", ""),
            started_at=float(data.get("started_at", 0)),
            session_id=data.get("session_id", ""),
            pinned=bool(data.get("pinned", False)),
            archived=bool(data.get("archived", False)),
        )


_NAME_SANITIZE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_name(raw: str) -> str:
    """tmux session names must avoid ':' and '.'; keep them shell-friendly."""
    name = _NAME_SANITIZE.sub("-", raw.strip()).strip("-")
    return name or "claude"


class Registry:
    """Multiple processes share this file (the sidebar, CLI invocations,
    tmux key bindings). Every mutation saves immediately, and maybe_reload()
    picks up other processes' writes cheaply via mtime, so callers must
    invoke it before reading or mutating."""

    def __init__(self, path: Path):
        self.path = path
        self.instances: list[Instance] = []
        self._loaded_stamp: tuple[int, int] | None = None
        self.load()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def maybe_reload(self) -> None:
        """Re-read the file if another process has written it since we last
        loaded/saved."""
        if self._stamp() != self._loaded_stamp:
            self.load()

    def load(self) -> None:
        self._loaded_stamp = self._stamp()
        try:
            data = json.loads(self.path.read_text())
            self.instances = [Instance.from_json(item) for item in data.get("instances", [])]
        except FileNotFoundError:
            self.instances = []
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Corrupt state file: preserve it for inspection, start fresh.
            backup = self.path.with_suffix(".json.corrupt")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self.instances = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"instances": [i.to_json() for i in self.instances]}, indent=2)
        )
        os.replace(tmp, self.path)  # atomic on POSIX
        self._loaded_stamp = self._stamp()

    # -- accessors ----------------------------------------------------------

    def get(self, name: str) -> Instance | None:
        return next((i for i in self.instances if i.name == name), None)

    def unique_name(self, base: str) -> str:
        base = sanitize_name(base)
        if not self.get(base):
            return base
        n = 2
        while self.get(f"{base}-{n}"):
            n += 1
        return f"{base}-{n}"

    # -- mutations ----------------------------------------------------------

    def add(self, instance: Instance) -> None:
        if self.get(instance.name):
            raise ValueError(f"instance {instance.name!r} already exists")
        self.instances.append(instance)
        self.save()

    def remove(self, name: str) -> None:
        self.instances = [i for i in self.instances if i.name != name]
        self.save()

    def rename(self, old: str, new: str) -> None:
        inst = self.get(old)
        if inst is None:
            raise KeyError(old)
        if self.get(new):
            raise ValueError(f"instance {new!r} already exists")
        inst.name = new
        self.save()

    def ordered(self, include_archived: bool = False) -> list["Instance"]:
        """Sidebar/selection order: pinned first (stable), archived last or
        hidden. This ordering is shared by the UI and `select N` so numbers
        always mean the same instance."""
        active = [i for i in self.instances if not i.archived]
        result = [i for i in active if i.pinned] + [i for i in active if not i.pinned]
        if include_archived:
            result += [i for i in self.instances if i.archived]
        return result

    def get_by_pane(self, pane_id: str) -> Instance | None:
        return next((i for i in self.instances if pane_id and i.pane_id == pane_id), None)

    def adopt_panes(self, panes: list[tuple[str, str]]) -> list[Instance]:
        """Register instance panes found on the server but missing from the
        registry (state file lost/corrupt). panes: (pane_id, window_name)."""
        adopted = []
        for pane_id, window_name in panes:
            if not self.get_by_pane(pane_id):
                inst = Instance(
                    name=self.unique_name(window_name),
                    cwd=os.path.expanduser("~"),
                    pane_id=pane_id,
                )
                self.instances.append(inst)
                adopted.append(inst)
        if adopted:
            self.save()
        return adopted

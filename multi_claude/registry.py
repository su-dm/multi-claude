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
    name: str  # doubles as the tmux session name
    cwd: str
    command: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Instance":
        return cls(
            name=data["name"],
            cwd=data["cwd"],
            command=list(data.get("command", [])),
            created_at=float(data.get("created_at", 0)),
        )


_NAME_SANITIZE = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_name(raw: str) -> str:
    """tmux session names must avoid ':' and '.'; keep them shell-friendly."""
    name = _NAME_SANITIZE.sub("-", raw.strip()).strip("-")
    return name or "claude"


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.instances: list[Instance] = []
        self.load()

    def load(self) -> None:
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

    def adopt_unknown_sessions(self, live_sessions: list[str]) -> list[Instance]:
        """Register sessions that exist on our tmux server but not in the
        registry (e.g. created by a script, or the state file was lost)."""
        adopted = []
        for session in live_sessions:
            if not self.get(session):
                inst = Instance(name=session, cwd=os.path.expanduser("~"))
                self.instances.append(inst)
                adopted.append(inst)
        if adopted:
            self.save()
        return adopted

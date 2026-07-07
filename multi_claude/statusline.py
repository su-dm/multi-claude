"""Capture Claude Code's own cost/usage numbers via its statusline hook.

Claude Code invokes the configured `statusLine.command` on every UI update,
passing session JSON on stdin — including `cost.total_cost_usd`, computed by
Claude Code itself with its own (always-current) pricing. That is strictly
better than our pricing-table estimate, so when the hook is installed the
dashboard shows CC's number (no "~" prefix) and the table is only a fallback
for sessions without captured data.

`multi-claude statusline` is the hook entry point: it records the JSON to
<data_dir>/costs/<session_id>.json and prints a normal statusline. If the
user already had a statusline command, install saves it and we delegate to
it, so their display is unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import Config

_MAX_COST_FILES = 400  # prune oldest beyond this; one file per session


def chain_path(config: Config) -> Path:
    return config.data_dir / "statusline-chain"


def record_and_render(config: Config, stdin_text: str) -> str:
    """Store the payload; return the statusline text to display."""
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        return "multi-claude: bad statusline payload"
    session_id = payload.get("session_id") or ""
    if session_id:
        config.costs_dir.mkdir(parents=True, exist_ok=True)
        target = config.costs_dir / f"{session_id}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "session_id": session_id,
            "total_cost_usd": (payload.get("cost") or {}).get("total_cost_usd"),
            "model": (payload.get("model") or {}).get("id")
            or (payload.get("model") or {}).get("display_name") or "",
            "updated_at": time.time(),
        }))
        os.replace(tmp, target)
        _prune(config.costs_dir)
    # Delegate to the user's original statusline command if we replaced one.
    chain = chain_path(config)
    if chain.exists():
        cmd = chain.read_text().strip()
        if cmd:
            try:
                proc = subprocess.run(
                    cmd, shell=True, input=stdin_text,
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0:
                    return proc.stdout.rstrip("\n")
            except (OSError, subprocess.TimeoutExpired):
                pass
    model = (payload.get("model") or {}).get("display_name") or ""
    cost = (payload.get("cost") or {}).get("total_cost_usd")
    cost_str = f" · ${cost:.2f}" if isinstance(cost, (int, float)) else ""
    return f"{model}{cost_str}"


def _prune(costs_dir: Path) -> None:
    try:
        files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-_MAX_COST_FILES]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def reported_cost(config: Config, session_id: str) -> float | None:
    """Claude Code's own cost figure for a session, if the hook captured it."""
    if not session_id:
        return None
    try:
        data = json.loads((config.costs_dir / f"{session_id}.json").read_text())
        value = data.get("total_cost_usd")
        return float(value) if value is not None else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def install(config: Config) -> str:
    """Wire our hook into ~/.claude/settings.json. An existing statusline
    command is preserved: saved to disk and chained after ours."""
    settings_path = config.claude_home / "settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError):
        settings = {}
    ours = f"{config.mc_command()} statusline"
    current = (settings.get("statusLine") or {}).get("command", "")
    if current and "multi_claude statusline" not in current:
        config.ensure_dirs()
        chain_path(config).write_text(current + "\n")
        note = f"(your previous statusline is preserved and still rendered: {current!r})"
    elif "multi_claude statusline" in current:
        note = "(already installed — refreshed)"
    else:
        note = "(no previous statusline; ours shows model + session cost)"
    settings["statusLine"] = {"type": "command", "command": ours}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, settings_path)
    return note


def uninstall(config: Config) -> str:
    """Remove our hook from ~/.claude/settings.json, restoring whatever
    statusline command it replaced (saved in the chain file)."""
    settings_path = config.claude_home / "settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "nothing to do (no readable settings.json)"
    current = (settings.get("statusLine") or {}).get("command", "")
    if "multi_claude statusline" not in current:
        return "nothing to do (our hook is not installed)"
    chain = chain_path(config)
    previous = chain.read_text().strip() if chain.exists() else ""
    if previous:
        settings["statusLine"] = {"type": "command", "command": previous}
        note = f"restored your previous statusline: {previous!r}"
    else:
        settings.pop("statusLine", None)
        note = "statusline hook removed"
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, settings_path)
    chain.unlink(missing_ok=True)
    return note


def run_hook(config: Config) -> int:
    print(record_and_render(config, sys.stdin.read()))
    return 0

"""Per-instance session info read from Claude Code's transcript files.

Claude Code writes one JSONL per session under
~/.claude/projects/<munged-cwd>/<session-id>.jsonl. From its tail we derive:

- context tokens: input + cache_read + cache_creation + output of the LAST
  assistant message (verified against Claude Code 2.1.x);
- model: `message.model` on the last assistant message;
- approximate cumulative cost: summed per-message usage x a pricing table
  (full-file scan, cached; marked approximate because pricing changes and
  cache-write TTL premiums are simplified);
- activity: a one-line "what is it doing" — the latest thinking snippet,
  tool call, or assistant text in the transcript.

Instance -> session-file matching is heuristic: prefer the file whose first
timestamp is closest to the instance's start time; fall back to the most
recently *written* file (resumed conversations append to their ORIGINAL
session file whose start predates the instance). Two instances started in
the same directory at nearly the same moment could theoretically swap files;
everything shown is informational, so that failure mode is acceptable.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MATCH_WINDOW_BEFORE = 120.0   # session appears to start before our spawn
MATCH_WINDOW_AFTER = 900.0    # session starts a while after spawn (slow boot)

_TAIL_BYTES = 256 * 1024

# $ per 1M tokens: (input, output, cache_read, cache_write_5m).
# Source: Anthropic pricing, cached 2026-06; approximate by design (Sonnet 5
# intro pricing, 1h-TTL cache writes, and server tools are not modeled).
_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-mythos": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-4": (5.0, 25.0, 0.5, 6.25),
    "claude-opus": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet": (3.0, 15.0, 0.3, 3.75),
    "claude-haiku": (1.0, 5.0, 0.1, 1.25),
}


def _pricing_for(model: str) -> tuple[float, float, float, float] | None:
    for prefix, rates in _PRICING.items():
        if model.startswith(prefix):
            return rates
    return None


@dataclass
class SessionInfo:
    session_id: str = ""
    tokens: int | None = None      # current context size
    model: str = ""                # e.g. "claude-sonnet-5"
    cost_usd: float | None = None  # cumulative session cost
    cost_source: str = "estimate"  # "claude" = CC-reported via statusline hook
    activity: str = ""             # latest thought/tool/text one-liner
    title: str = ""                # Claude Code's own AI session title


def project_dir(claude_home: Path, cwd: str) -> Path:
    """Claude Code munges the working directory into a flat dir name."""
    return claude_home / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def _parse_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def session_start_time(path: Path) -> float | None:
    """First top-level timestamp in the file (fallback: file mtime)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(25):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = obj.get("timestamp")
                if isinstance(ts, str):
                    parsed = _parse_ts(ts)
                    if parsed is not None:
                        return parsed
        return os.stat(path).st_mtime
    except OSError:
        return None


def find_session_file(claude_home: Path, cwd: str, started_at: float) -> Path | None:
    """The session transcript belonging to an instance started at started_at."""
    pdir = project_dir(claude_home, cwd)
    try:
        candidates = list(pdir.glob("*.jsonl"))
    except OSError:
        return None
    best: tuple[float, Path] | None = None
    for path in candidates:
        start = session_start_time(path)
        if start is None:
            continue
        delta = start - started_at
        if -MATCH_WINDOW_BEFORE <= delta <= MATCH_WINDOW_AFTER:
            score = abs(delta)
            if best is None or score < best[0]:
                best = (score, path)
    if best:
        return best[1]
    # Resumed conversations (claude --continue/--resume) append to the
    # ORIGINAL session file, whose start time predates this instance. Fall
    # back to the transcript most recently written since the instance started.
    recent: tuple[float, Path] | None = None
    for path in candidates:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if mtime >= started_at - 5 and (recent is None or mtime > recent[0]):
            recent = (mtime, path)
    return recent[1] if recent else None


def _usage_total(usage: dict) -> int:
    return sum(
        usage.get(k) or 0
        for k in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )


def _activity_from_message(msg: dict) -> str:
    """One line describing the newest content block of an assistant message.
    Later block types win (a tool call after text = currently acting)."""
    result = ""
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking" and block.get("thinking"):
            result = "∴ " + _first_line(block["thinking"])
        elif block.get("type") == "text" and block.get("text"):
            result = _first_line(block["text"])
        elif block.get("type") == "tool_use":
            result = _describe_tool(block.get("name", "?"), block.get("input") or {})
    return result


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def _describe_tool(name: str, tool_input: dict) -> str:
    """'Edit ui.py' beats a raw JSON dump in 30 columns."""
    arg = ""
    for key in ("file_path", "path", "pattern", "command", "description", "query", "url", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            arg = value.strip().splitlines()[0]
            if key in ("file_path", "path"):
                arg = os.path.basename(arg)
            break
    return f"{name}: {arg}" if arg else name


def read_session_info(path: Path) -> SessionInfo:
    """Parse the tail of a transcript into a SessionInfo (minus cost)."""
    info = SessionInfo(session_id=path.stem)
    try:
        size = os.stat(path).st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return info
    for line in reversed(tail.splitlines()):
        if info.tokens is not None and info.activity and info.title:
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # first line of the tail window may be truncated
        if not info.title and obj.get("type") == "ai-title":
            info.title = str(obj.get("aiTitle") or "")
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        if not info.model and msg.get("model"):
            info.model = msg["model"]
        if not info.activity:
            info.activity = _activity_from_message(msg)
        if info.tokens is None:
            total = _usage_total(msg.get("usage") or {})
            if total > 0:
                info.tokens = total
    return info


def session_cost(path: Path) -> float | None:
    """Approximate cumulative cost: sum usage of every assistant message.
    Full-file scan — call rarely (cached by TokenReader per mtime)."""
    total = 0.0
    found = False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                rates = _pricing_for(msg.get("model") or "")
                if rates is None:
                    continue
                in_rate, out_rate, cread_rate, cwrite_rate = rates
                total += (
                    (usage.get("input_tokens") or 0) * in_rate
                    + (usage.get("output_tokens") or 0) * out_rate
                    + (usage.get("cache_read_input_tokens") or 0) * cread_rate
                    + (usage.get("cache_creation_input_tokens") or 0) * cwrite_rate
                ) / 1_000_000
                found = True
    except OSError:
        return None
    return total if found else None


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_cost(usd: float | None) -> str:
    if usd is None:
        return ""
    if usd < 0.995:
        return f"{usd * 100:.0f}¢"
    return f"${usd:.2f}" if usd < 100 else f"${usd:.0f}"


def fmt_model(model: str) -> str:
    """'claude-sonnet-5' -> 'sonnet-5'; unknown strings pass through."""
    return model.removeprefix("claude-") or model


@dataclass
class _CacheEntry:
    path: Path
    mtime_ns: int = -1
    info: SessionInfo = field(default_factory=SessionInfo)


class TokenReader:
    """Polls session info with per-instance caching: the session file is
    resolved once per (instance, start time) and re-read only when its mtime
    changes, so this is cheap enough for the 1 s poll loop."""

    def __init__(self, claude_home: Path):
        self.claude_home = claude_home
        self._cache: dict[tuple[str, float], _CacheEntry] = {}

    def info_for(self, name: str, cwd: str, started_at: float) -> SessionInfo | None:
        key = (name, started_at)
        entry = self._cache.get(key)
        if entry is None or not entry.path.exists():
            path = find_session_file(self.claude_home, cwd, started_at)
            if path is None:
                return None
            entry = _CacheEntry(path=path)
            self._cache[key] = entry
        try:
            mtime_ns = os.stat(entry.path).st_mtime_ns
        except OSError:
            self._cache.pop(key, None)
            return None
        if mtime_ns != entry.mtime_ns:
            info = read_session_info(entry.path)
            info.cost_usd = session_cost(entry.path)
            entry.info = info
            entry.mtime_ns = mtime_ns
        return entry.info

    def tokens_for(self, name: str, cwd: str, started_at: float) -> int | None:
        info = self.info_for(name, cwd, started_at)
        return info.tokens if info else None

    def forget(self, name: str) -> None:
        for key in [k for k in self._cache if k[0] == name]:
            self._cache.pop(key, None)

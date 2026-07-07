"""Measure the CPU/RAM overhead of multi-claude's own processes.

"Overhead" = everything multi-claude adds on top of the claude processes the
user would be running anyway: the dedicated tmux server, the sidebar UI (with
its poll loop), the welcome placeholder, and the keep-alive sleep. Claude
instances themselves are reported separately for contrast, not counted as
overhead.

CPU is sampled from /proc/<pid>/stat utime+stime over a real interval (the
cumulative average since process start would understate a poll loop's steady
cost). RSS comes from /proc/<pid>/status VmRSS.
"""

from __future__ import annotations

import os
import resource
import time
from dataclasses import dataclass

_CLK_TCK = os.sysconf("SC_CLK_TCK")


@dataclass
class ProcSample:
    pid: int
    label: str
    rss_kib: int = 0
    cpu_percent: float = 0.0


def _cpu_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().rsplit(") ", 1)[1].split()
        # fields[11]=utime, fields[12]=stime (0-indexed after comm)
        return int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return None


def _rss_kib(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return 0


def _children(pid: int) -> list[int]:
    kids = []
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            path = f"/proc/{pid}/task/{tid}/children"
            with open(path) as fh:
                kids += [int(c) for c in fh.read().split()]
    except OSError:
        pass
    return kids


def _tree(pid: int) -> list[int]:
    pids, queue = [], [pid]
    while queue:
        p = queue.pop()
        pids.append(p)
        queue += _children(p)
    return pids


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _label(cmd: str) -> str:
    if "multi_claude sidebar" in cmd:
        return "sidebar (poll loop)"
    if "multi_claude welcome" in cmd:
        return "welcome pane"
    if cmd.startswith("tmux"):
        return "tmux server"
    if "sleep infinity" in cmd:
        return "keep-alive sleep"
    return "helper: " + cmd[:50]


def measure(manager, interval: float = 2.0) -> tuple[list[ProcSample], list[ProcSample]]:
    """Returns (overhead_samples, claude_samples), CPU sampled over interval."""
    server_pid_out = manager.tmux._run(
        "display-message", "-p", "#{pid}", check=False
    ).stdout.strip()
    if not server_pid_out.isdigit():
        return [], []
    overhead: list[ProcSample] = []
    claude: list[ProcSample] = []
    for pid in _tree(int(server_pid_out)):
        cmd = _cmdline(pid)
        if not cmd:
            continue
        sample = ProcSample(pid=pid, label=_label(cmd))
        is_claude = (
            "claude" in cmd
            and "multi_claude" not in cmd
            and not cmd.startswith("tmux")
        )
        if is_claude:
            sample.label = "claude: " + cmd.split()[0].rsplit("/", 1)[-1]
            claude.append(sample)
        else:
            overhead.append(sample)
    before = {s.pid: _cpu_ticks(s.pid) for s in overhead + claude}
    time.sleep(interval)
    for sample in overhead + claude:
        b, a = before.get(sample.pid), _cpu_ticks(sample.pid)
        if b is not None and a is not None:
            sample.cpu_percent = 100.0 * (a - b) / _CLK_TCK / interval
        sample.rss_kib = _rss_kib(sample.pid)
    return overhead, claude


def print_stats(manager, interval: float = 2.0) -> None:
    print(f"sampling CPU over {interval:.0f}s ...", flush=True)
    overhead, claude = measure(manager, interval)
    if not overhead:
        print("multi-claude server is not running")
        return

    def show(rows: list[ProcSample]) -> tuple[float, float]:
        cpu = rss = 0.0
        for s in sorted(rows, key=lambda r: -r.rss_kib):
            print(f"  {s.label:<28} pid {s.pid:>7}  {s.cpu_percent:5.1f}% cpu  {s.rss_kib / 1024:7.1f} MiB")
            cpu += s.cpu_percent
            rss += s.rss_kib
        return cpu, rss / 1024

    print("\nmulti-claude overhead (dashboard machinery):")
    cpu, rss = show(overhead)
    print(f"  {'TOTAL OVERHEAD':<28} {'':>11}  {cpu:5.1f}% cpu  {rss:7.1f} MiB")
    if claude:
        print("\nclaude instances (yours — not overhead):")
        ccpu, crss = show(claude)
        print(f"  {'TOTAL CLAUDE':<28} {'':>11}  {ccpu:5.1f}% cpu  {crss:7.1f} MiB")
    print(f"\npoll loop true cost (incl. forked tmux/git): {poll_cost_percent(manager):.1f}% of one core")


def poll_cost_percent(manager, cycles: int = 10) -> float:
    """CPU of one status-poll cycle, measured in-process via rusage so the
    short-lived tmux/git subprocesses (invisible to /proc sampling of
    long-lived pids) are counted. Returns %-of-one-core at 1 poll/s."""
    manager.poll_once()  # warm caches
    s0 = resource.getrusage(resource.RUSAGE_SELF)
    c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    for _ in range(cycles):
        manager.poll_once()
    s1 = resource.getrusage(resource.RUSAGE_SELF)
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (
        (s1.ru_utime + s1.ru_stime - s0.ru_utime - s0.ru_stime)
        + (c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime)
    )
    return 100.0 * cpu / cycles / manager.config.poll_interval

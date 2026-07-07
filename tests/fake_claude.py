#!/usr/bin/env python3
"""Stand-in for the claude binary used by tests/smoke.sh.

Mimics the on-screen states multi-claude's heuristics look for, without
needing a real Claude Code install or API access: a busy spinner frame, then
a prompt box; echoes each line it receives and returns to the prompt.
"""

import sys
import time

# First numeric arg = busy duration; claude-style flags (--continue,
# --resume <id>) are accepted and ignored, like the real binary would.
BUSY_SECONDS = next(
    (float(a) for a in sys.argv[1:] if a.replace(".", "", 1).isdigit()), 2.0
)


def clear() -> None:
    # Real Claude Code redraws its TUI in place (the spinner line disappears
    # when work finishes); emulate that so only the current state is visible.
    print("\x1b[2J\x1b[H", end="")


def busy(seconds: float) -> None:
    clear()
    print(f"✻ Fabricating… (esc to interrupt · {seconds:.0f}s)", flush=True)
    time.sleep(seconds)
    clear()  # spinner disappears once the work is done, like the real TUI


def prompt() -> None:
    # No clear here: responses printed before the prompt stay visible,
    # exactly like real transcript text above Claude's input box.
    print("╭──────────────────────────────╮")
    print("│ >                            │")
    print("╰──────────────────────────────╯")
    print("  ? for shortcuts", flush=True)


def main() -> None:
    busy(BUSY_SECONDS)
    prompt()
    for line in sys.stdin:
        line = line.strip()
        if line in ("exit", "/exit"):
            print("bye", flush=True)
            return
        busy(BUSY_SECONDS)
        print(f"● you said: {line}", flush=True)
        prompt()


if __name__ == "__main__":
    main()

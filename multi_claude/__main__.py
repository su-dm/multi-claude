"""CLI entry point.

`multi-claude` with no arguments boots the dashboard (a tmux session with a
sidebar pane and a live Claude pane) and attaches to it. Subcommands provide
a scripting interface over the same instance registry/tmux server; a few
(`sidebar`, `welcome`, `select`) are internal hooks used by the dashboard's
own panes and key bindings.
"""

from __future__ import annotations

import argparse
import locale
import shutil
import sys
import time

from . import CLAUDE_CODE_VERIFIED, __version__
from .config import Config
from .manager import InstanceManager
from .tmux import TmuxError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-claude",
        description="Manage multiple Claude Code instances from one terminal dashboard.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"multi-claude {__version__} (verified against Claude Code {CLAUDE_CODE_VERIFIED}.x)",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_new = sub.add_parser(
        "new",
        help="create an instance without opening the dashboard",
        epilog="args after a lone -- are passed to claude, e.g.: "
        "multi-claude new ~/proj -- --continue",
    )
    p_new.add_argument("directory", help="working directory for the instance")
    p_new.add_argument("-n", "--name", help="instance name (default: directory basename)")
    p_new.add_argument(
        "-w", "--worktree", metavar="BRANCH",
        help="isolate the agent on a git worktree of DIRECTORY for BRANCH "
        "(created under <repo>.worktrees/<branch>)",
    )

    sub.add_parser("ls", help="list instances and their status")

    p_attach = sub.add_parser("attach", help="open the dashboard showing this instance")
    p_attach.add_argument("name")

    p_send = sub.add_parser("send", help="send a message to an instance")
    p_send.add_argument("name")
    p_send.add_argument("text")

    p_kill = sub.add_parser("kill", help="kill an instance")
    p_kill.add_argument("name")

    p_resume = sub.add_parser(
        "resume", help="relaunch an exited instance, continuing its conversation"
    )
    p_resume.add_argument("name")

    sub.add_parser(
        "resume-all",
        help="after a reboot: relaunch every exited instance, continuing conversations",
    )

    sub.add_parser("stats", help="CPU/RAM overhead of multi-claude's own processes")

    p_arch = sub.add_parser(
        "archive", help="kill an agent but keep it revivable (hidden from sidebar)"
    )
    p_arch.add_argument("name")
    p_unarch = sub.add_parser(
        "unarchive", help="revive an archived agent, continuing its conversation"
    )
    p_unarch.add_argument("name")
    p_pin = sub.add_parser("pin", help="toggle pinning an instance to the sidebar top")
    p_pin.add_argument("name")

    p_notify = sub.add_parser("notify", help="turn attention notifications on/off")
    p_notify.add_argument("state", choices=["on", "off", "status"])

    sub.add_parser(
        "install-statusline",
        help="capture Claude Code's exact per-session cost via its statusline "
        "hook (replaces pricing estimates; preserves an existing statusline)",
    )
    sub.add_parser("statusline", help=argparse.SUPPRESS)  # the hook itself

    sub.add_parser("bootstrap", help="create the dashboard session without attaching")
    sub.add_parser(
        "refresh-ui",
        help="restart sidebar/welcome after upgrading (instances untouched)",
    )

    # Internal hooks (used by the dashboard's own panes / tmux bindings):
    p_select = sub.add_parser("select", help=argparse.SUPPRESS)
    p_select.add_argument("which", help="instance number, name, 'next' or 'prev'")
    sub.add_parser("sidebar", help=argparse.SUPPRESS)
    sub.add_parser("welcome", help=argparse.SUPPRESS)

    return parser


def run_welcome() -> None:
    """Placeholder process for the viewer slot before any instance exists."""
    print(
        "\n  multi-claude\n"
        "  ────────────\n"
        "  No instance selected.\n\n"
        "  Sidebar keys (focus it with Alt-h):\n"
        "    n         new instance (Tab completes directories)\n"
        "    Enter     show + focus the selected instance\n"
        "    ?         full key reference\n\n"
        "  From anywhere in the dashboard:\n"
        "    Alt-1..9  switch instance   Alt-o  next instance\n"
        "    Alt-h/l   move focus        Alt-z  zoom this pane\n"
        "    C-q       detach (everything keeps running)\n",
        flush=True,
    )
    while True:  # keep the pane alive; content is static
        time.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    if argv is None:
        argv = sys.argv[1:]
    # Everything after a lone "--" is forwarded to claude verbatim (only
    # meaningful for `new`); argparse never sees it.
    claude_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, claude_args = argv[:split], argv[split + 1 :]
    args = build_parser().parse_args(argv)

    if shutil.which("tmux") is None:
        print("multi-claude requires tmux (sudo apt install tmux)", file=sys.stderr)
        return 1

    manager = InstanceManager(Config())
    try:
        if args.cmd is None:
            manager.bootstrap_dashboard()
            return manager.tmux.attach_dashboard()
        if args.cmd == "bootstrap":
            manager.bootstrap_dashboard()
            print("dashboard ready (multi-claude to attach)")
            return 0
        if args.cmd == "refresh-ui":
            manager.refresh_ui()
            print("dashboard UI restarted (instances untouched)")
            return 0
        if args.cmd == "sidebar":
            from .ui import run_sidebar

            run_sidebar(manager)
            return 0
        if args.cmd == "welcome":
            run_welcome()
            return 0
        if args.cmd == "select":
            manager.select(args.which)
            return 0
        if args.cmd == "new":
            inst = manager.create(
                args.directory, args.name, claude_args, worktree_branch=args.worktree
            )
            print(f"created {inst.name} in {inst.cwd}  (multi-claude attach {inst.name})")
            return 0
        if args.cmd == "ls":
            manager.poll_once()
            snaps = manager.snapshots(include_archived=True)
            if not snaps:
                print("no instances")
                return 0
            from .transcripts import fmt_tokens

            for snap in snaps:
                detail = f" ({snap.status.detail})" if snap.status.detail else ""
                shown = " *" if snap.displayed else "  "
                status = "archived" if snap.instance.archived else snap.status.status.value
                tokens = fmt_tokens(snap.tokens) or "-"
                pin = "^" if snap.instance.pinned else " "
                print(
                    f"{snap.instance.name:<24}{shown}{pin}"
                    f"{status:<10}{tokens:>6}  "
                    f"{snap.instance.cwd}{detail}"
                )
            return 0
        if args.cmd == "attach":
            manager.bootstrap_dashboard()
            manager.display(args.name, focus=True)
            return manager.tmux.attach_dashboard()
        if args.cmd == "send":
            manager.send_text(args.name, args.text)
            return 0
        if args.cmd == "kill":
            manager.kill(args.name)
            return 0
        if args.cmd == "resume":
            manager.restart(args.name, resume=True)
            print(f"resumed {args.name}")
            return 0
        if args.cmd == "resume-all":
            count = 0
            for snap in manager.snapshots_fresh():
                if not snap.pane_alive or snap.status.status.value == "exited":
                    manager.restart(snap.instance.name, resume=True)
                    print(f"resumed {snap.instance.name}")
                    count += 1
            print(f"{count} instance(s) resumed" if count else "nothing to resume")
            return 0
        if args.cmd == "stats":
            from .overhead import print_stats

            print_stats(manager)
            return 0
        if args.cmd == "archive":
            manager.archive(args.name)
            print(f"archived {args.name} (unarchive to bring it back)")
            return 0
        if args.cmd == "unarchive":
            manager.unarchive(args.name)
            print(f"unarchived {args.name} (conversation resumed)")
            return 0
        if args.cmd == "pin":
            pinned = manager.toggle_pin(args.name)
            print(f"{'pinned' if pinned else 'unpinned'} {args.name}")
            return 0
        if args.cmd == "notify":
            if args.state == "status":
                print("notifications " + ("on" if manager.config.notify else "off"))
            else:
                manager.config.notify = args.state == "on"
                manager.config.save_setting("notify", manager.config.notify)
                print(f"notifications {args.state}")
            return 0
        if args.cmd == "statusline":
            from .statusline import run_hook

            return run_hook(manager.config)
        if args.cmd == "install-statusline":
            from .statusline import install

            note = install(manager.config)
            print("statusline hook installed " + note)
            print("new/restarted claude sessions will report exact costs")
            return 0
    except (ValueError, KeyError, TmuxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

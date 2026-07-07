"""CLI entry point.

`multi-claude` with no arguments opens the dashboard. Subcommands provide a
scripting interface over the same instance registry/tmux server, so the tool
is automatable (and testable) without the TUI.
"""

from __future__ import annotations

import argparse
import locale
import shutil
import sys

from . import __version__
from .config import Config
from .manager import InstanceManager
from .tmux import TmuxError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-claude",
        description="Manage multiple Claude Code instances from one terminal dashboard.",
    )
    parser.add_argument("--version", action="version", version=f"multi-claude {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_new = sub.add_parser(
        "new",
        help="create an instance without opening the dashboard",
        epilog="args after a lone -- are passed to claude, e.g.: "
        "multi-claude new ~/proj -- --continue",
    )
    p_new.add_argument("directory", help="working directory for the instance")
    p_new.add_argument("-n", "--name", help="instance name (default: directory basename)")

    sub.add_parser("ls", help="list instances and their status")

    p_attach = sub.add_parser("attach", help="attach to an instance's terminal")
    p_attach.add_argument("name")

    p_send = sub.add_parser("send", help="send a message to an instance")
    p_send.add_argument("name")
    p_send.add_argument("text")

    p_kill = sub.add_parser("kill", help="kill an instance")
    p_kill.add_argument("name")

    return parser


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
            from .ui import run_dashboard

            run_dashboard(manager)
            return 0
        if args.cmd == "new":
            inst = manager.create(args.directory, args.name, claude_args)
            print(f"created {inst.name} in {inst.cwd}  (multi-claude attach {inst.name})")
            return 0
        if args.cmd == "ls":
            manager.poll_once()
            snaps = manager.snapshots()
            if not snaps:
                print("no instances")
                return 0
            for snap in snaps:
                detail = f" ({snap.status.detail})" if snap.status.detail else ""
                print(f"{snap.instance.name:<24} {snap.status.status.value:<18}{snap.instance.cwd}{detail}")
            return 0
        if args.cmd == "attach":
            if not manager.tmux.has_session(args.name):
                print(f"no running session named {args.name!r}", file=sys.stderr)
                return 1
            return manager.attach(args.name)
        if args.cmd == "send":
            manager.send_text(args.name, args.text)
            return 0
        if args.cmd == "kill":
            if manager.registry.get(args.name) is None:
                print(f"no instance named {args.name!r}", file=sys.stderr)
                return 1
            manager.kill(args.name)
            return 0
    except (ValueError, KeyError, TmuxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

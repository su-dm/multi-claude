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
        "hook (wired by install.sh; preserves an existing statusline)",
    )
    sub.add_parser(
        "uninstall-statusline",
        help="remove the cost hook from ~/.claude/settings.json, restoring "
        "any previous statusline (costs fall back to estimates)",
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
    sub.add_parser("help-popup", help=argparse.SUPPRESS)
    sub.add_parser("quit", help=argparse.SUPPRESS)  # C-c on a dead pane

    return parser


def run_welcome() -> None:
    """Placeholder process for the viewer slot before any instance exists."""
    mac = sys.platform == "darwin"
    mod = "⌥" if mac else "Alt-"
    mac_hint = (
        "  ⌥ keys need Option to send Esc+/Meta (else they\n"
        "  type accents instead of reaching tmux):\n"
        "    iTerm2:   Settings→Profiles→Keys→General\n"
        "              → Left Option key: Esc+\n"
        "    Terminal: Settings→Profiles→Keyboard\n"
        "              → Use Option as Meta key\n"
        "  Meanwhile C-b o (tmux prefix) switches panes.\n"
        if mac
        else ""
    )
    print(
        "\n  multi-claude\n"
        "  ────────────\n"
        "  No instance selected.\n\n"
        f"  Sidebar keys (focus it with {mod}h):\n"
        "    n         new instance (Tab completes directories)\n"
        "    Enter     show + focus the selected instance\n"
        "    ?         full key reference\n\n"
        "  From anywhere in the dashboard:\n"
        f"    {mod + '1..9':<8}  switch instance   {mod + 'o':<5} next instance\n"
        f"    {mod + 'h/' + mod + 'l':<8}  move focus        {mod + 'z':<5} zoom this pane\n"
        f"    {'C-q':<8}  detach            {'C-c':<5} quit dashboard\n"
        "              (either way, agents keep running)\n\n"
        + mac_hint,
        flush=True,
    )
    while True:  # keep the pane alive; content is static
        time.sleep(3600)


def run_help_popup() -> None:
    """Render the key reference inside a tmux popup. Normally piped into
    `less` (which scrolls and holds the popup open); when stdout is the
    popup's tty directly (no less), wait for a keypress ourselves."""
    from .ui import help_text

    print(help_text(), flush=True)
    if not sys.stdout.isatty():
        return
    print("  any key closes", flush=True)
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        try:
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, OSError):
        try:
            input()
        except EOFError:
            pass


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
            rc = manager.tmux.attach_dashboard()
            # A C-c quit kills the session under the client, which then exits
            # nonzero — that's a clean shutdown, not an error.
            return 0 if rc != 0 and not manager.tmux.dashboard_exists() else rc
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
            try:
                run_welcome()
            except KeyboardInterrupt:
                # C-c with the welcome pane focused: same graceful quit as
                # C-c in the sidebar (a dead placeholder pane helps nobody).
                manager.shutdown_dashboard()
            return 0
        if args.cmd == "help-popup":
            run_help_popup()
            return 0
        if args.cmd == "quit":
            # tmux C-c binding on a dead pane: same graceful shutdown as C-c
            # in the sidebar (park the displayed agent, kill the dashboard).
            manager.shutdown_dashboard()
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
            rc = manager.tmux.attach_dashboard()
            return 0 if rc != 0 and not manager.tmux.dashboard_exists() else rc
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
        if args.cmd == "uninstall-statusline":
            from .statusline import uninstall

            print(uninstall(manager.config))
            print("dashboard costs fall back to pricing estimates (~ prefix)")
            return 0
    except (ValueError, KeyError, TmuxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

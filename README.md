# multi-claude

A terminal dashboard for running and supervising **multiple Claude Code
instances** on Linux. A sidebar lists every instance with a live three-state
status — **working / idle / help** (help = Claude is waiting on you: a
permission prompt, a plan approval, a question) — and the selected
instance's *real, fully interactive* Claude session sits right next to it.
You type into Claude directly while the sidebar stays visible.

Inspired by [cmux](https://github.com/manaflow-ai/cmux) (Electron, macOS),
but terminal-native: it runs in any terminal, works over SSH, and instances
survive dashboard restarts because tmux owns them, not the UI.

```
 multi-claude                     │ ● Done! All 34 tests pass. Next I'll
 ❯ ◐ 1 backend                    │   wire up the retry logic.
     ~/code/backend · Refactoring…│
   ● 2 frontend                   │ ╭──────────────────────────────────╮
     ~/code/frontend · idle       │ │ > fix the flaky websocket test█   │
   ◆ 3 infra                      │ ╰──────────────────────────────────╯
     ~/code/infra · help          │   ? for shortcuts
 ↵ open · n new · ? help          │            ← this is the real Claude
```

## Requirements

- Linux (developed on Ubuntu), Python ≥ 3.10 (stdlib only — no pip deps)
- tmux ≥ 3.x
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI

## Install

```bash
./install.sh            # checks deps, symlinks into ~/.local/bin
multi-claude            # open the dashboard
```

Or `pipx install .` if you prefer a managed install. `make uninstall`
removes the symlink.

## Usage

The dashboard is a tmux window on a dedicated server (your own tmux setup is
untouched): a fixed sidebar pane plus a viewer slot holding the selected
instance's actual pane. Focus follows tmux rules — when the Claude pane is
focused you are simply *in* Claude Code: full TUI, scrollback, mouse.

### Keys that work anywhere in the dashboard (no prefix)

| Key | Action |
| --- | --- |
| `Alt-h` / `Alt-l` | focus sidebar / Claude pane |
| `Alt-1`…`Alt-9` | display instance N |
| `Alt-o` | cycle to the next instance |
| `Alt-z` | zoom the focused pane full-screen (again to restore) |
| `C-q` | detach — dashboard and all instances keep running |

### Sidebar keys (vim flavored)

| Key | Action |
| --- | --- |
| `j` / `k`, `g` / `G` | move selection |
| `Enter` / `l` | display selected instance and focus it |
| `1`–`9` | display instance N |
| `n` | new instance — **Tab autocompletes directories** in the prompt |
| `i` | send a one-line message without moving focus |
| `x` | kill instance (asks for confirmation) |
| `R` | restart an exited instance (same directory & args) |
| `r` | rename instance |
| `q` | detach (same as `C-q`) |
| `?` | help overlay |

When an instance transitions from working to idle or help, the dashboard
rings the terminal bell and (if `notify-send` exists) posts a desktop
notification. Disable with `MULTI_CLAUDE_NOTIFY=0`.

### CLI (scripting interface)

```bash
multi-claude new ~/code/backend -n backend    # spawn without opening the UI
multi-claude new ~/code/api -- --continue     # args after -- go to claude
multi-claude ls                               # status; '*' marks displayed
multi-claude send backend "run the tests"     # type into an instance
multi-claude attach backend                   # open dashboard on an instance
multi-claude kill backend
multi-claude bootstrap                        # build dashboard, don't attach
```

### Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `MULTI_CLAUDE_CLAUDE_CMD` | `claude` | binary to launch (resolved to an absolute path) |
| `MULTI_CLAUDE_SOCKET` | `multi-claude` | tmux socket name (isolates the server) |
| `MULTI_CLAUDE_DATA_DIR` | `~/.local/share/multi-claude` | registry + generated tmux.conf |
| `MULTI_CLAUDE_POLL_INTERVAL` | `1.0` | seconds between status polls |
| `MULTI_CLAUDE_NOTIFY` | `1` | `0` disables bell/desktop notifications |

## Design

### Why a tmux backend instead of embedding terminals (like cmux)?

Each instance is a tmux *pane* (tracked by its immutable pane id) on a
dedicated server (`tmux -L multi-claude`, own config). Undisplayed instances
park as windows of a hidden `mc-work` session; selecting one `swap-pane`s it
into the dashboard's viewer slot. Because the thing next to the sidebar *is*
the live pane, there is no preview/attach split, no embedded terminal
emulator to maintain, and rendering fidelity is exact — tmux is the terminal
emulator. Compared to cmux this also buys:

- **Crash isolation / persistence**: the UI is stateless; kill it, restart
  it, reattach from another SSH session — instances are unaffected.
- **Zero heavyweight deps**: no Electron, no Node; stdlib Python + tmux.
- **Composability**: everything is scriptable (`multi-claude send`, plain
  `tmux -L multi-claude …`).

### Status detection (working / idle / help) & contingencies

`multi_claude/status.py` is the only module that knows what Claude Code's UI
looks like (verified against 2.1.x). Two layers:

1. **Markers** on the visible screen: a spinner line containing
   `esc to interrupt` → *working*; an option list with a `❯` cursor or an
   `Enter to confirm` / `Do you want…` footer → *help* (permissions, plan
   approval, trust prompt, questions); the `│ >` input box → *idle*.
2. **Screen-change fallback**: the poller hashes each pane's visible text;
   a changing screen with no dialog counts as *working*, a static
   unrecognized one as *idle*. So even if a future Claude Code changes every
   string, the three states stay approximately right instead of breaking.

A dead pane means *exited* (kept via `remain-on-exit` so you can read the
last output and press `R` to restart). Marker fixtures live in
`tests/test_status.py`.

Other contingencies handled deliberately:

- **nvm-installed claude**: the absolute path is resolved at spawn time.
- **Nested tmux**: attaching unsets `$TMUX`; all dashboard bindings are
  Alt-based and prefix-less, so they pass through your outer tmux untouched.
- **Multi-process state**: the sidebar, CLI calls, and tmux key bindings all
  share the registry file; every writer saves atomically and every reader
  reloads on mtime change.
- **Corrupt/lost state file**: preserved as `.corrupt` and rebuilt; instance
  panes found on the server but missing from the registry are adopted,
  never killed.
- **Sidebar crash**: `remain-on-exit` keeps the traceback visible; the next
  `multi-claude` invocation respawns dead dashboard panes.

## Development

```bash
make test    # unit tests (status, registry, tmux argv, dir completion)
make smoke   # integration: real tmux + tests/fake_claude.py stand-in
make check   # both
```

The smoke test runs on an isolated socket and data dir and needs no Claude
install or network. See `JOURNAL.md` for the development log and backlog.

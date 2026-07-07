# multi-claude

A terminal dashboard for running and supervising **multiple Claude Code
instances** on Linux. Sidebar on the left lists every instance with a live
status (working / awaiting your message / needs input / exited) and its
directory; the right side shows a live preview of the selected instance.
Press Enter to drop into the real session; `C-q` brings you back.

Inspired by [cmux](https://github.com/manaflow-ai/cmux) (Electron, macOS),
but terminal-native: it runs in any terminal, works over SSH, and instances
survive dashboard restarts because tmux owns them, not the UI.

```
 multi-claude  3 instance(s)
 ◐ 1 backend                       │ backend — ~/code/backend
   ~/code/backend · Refactoring…   │ [working: Refactoring…]
 ● 2 frontend                      │
   ~/code/frontend · awaiting msg  │  ● Done! All 34 tests pass.
 ◆ 3 infra                         │  ╭─────────────────────────╮
   ~/code/infra · needs input      │  │ >                       │
                                   │  ╰─────────────────────────╯
 j/k move · Enter attach · n new · i send · x kill · R restart · ? help
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

Or `pipx install .` if you prefer a managed install. `make uninstall` removes
the symlink. Running instances live on a tmux server independent of the
dashboard: `tmux -L multi-claude ls` shows them raw if you ever need to.

## Usage

### Dashboard keys (vim/tmux flavored)

| Key | Action |
| --- | --- |
| `j` / `k` (or arrows) | move selection |
| `g` / `G` | first / last instance |
| `1`–`9` | jump to instance N |
| `Enter` / `l` / `o` | attach to the selected instance |
| `n` | new instance (prompts for directory, then name) |
| `i` | send a one-line message to the instance *without* attaching |
| `x` | kill instance (asks for confirmation) |
| `R` | restart an exited instance (same directory & args) |
| `r` | rename instance |
| `?` | help overlay |
| `q` | quit the dashboard — **instances keep running** |

Inside an attached session, **`C-q` detaches** back to the dashboard (no
prefix needed). Everything else is the untouched Claude Code TUI, scrollback
and mouse included.

When an instance stops working and starts waiting on you, the dashboard
rings the terminal bell and (if `notify-send` exists) posts a desktop
notification. Disable with `MULTI_CLAUDE_NOTIFY=0`.

### CLI (scripting interface)

```bash
multi-claude new ~/code/backend -n backend    # spawn without opening the UI
multi-claude new ~/code/api -- --continue     # args after -- go to claude
multi-claude ls                               # names, statuses, directories
multi-claude send backend "run the tests"     # type into an instance
multi-claude attach backend                   # attach directly
multi-claude kill backend
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

Three options were weighed:

1. **Embed PTY + terminal emulator in the app** (cmux's approach, via
   xterm.js). Full control, but Claude Code is a heavy TUI (alt-screen,
   256-color, mouse, constant redraws); re-implementing terminal emulation is
   the riskiest part of the whole system, and instances die with the app.
2. **Pure tmux scripting** — no real UI, status handling becomes duct tape.
3. **TUI dashboard + tmux as the session backend** *(chosen)*. Each instance
   is a detached session on a dedicated tmux server (`tmux -L multi-claude`,
   own config — your tmux setup is never touched). The dashboard polls
   `capture-pane` for status/preview and *attaches* for interaction, so
   rendering fidelity is perfect because tmux **is** the terminal emulator.

What this buys over cmux, besides being Linux/terminal-native:

- **Crash isolation / persistence**: the UI is stateless; kill it, restart
  it, run it from another SSH session — instances are unaffected.
- **Zero heavyweight deps**: no Electron, no Node; stdlib Python + tmux.
- **Composability**: everything is scriptable (`multi-claude send`, plain
  `tmux -L multi-claude …`), so it plugs into shell workflows.

Trade-offs accepted:

- The preview pane is a plain-text snapshot (colors stripped), refreshed
  every poll interval; full fidelity is one `Enter` away.
- Status detection is heuristic string matching on the visible pane (see
  below).

### Status detection & contingencies

`multi_claude/status.py` is the only module that knows what Claude Code's UI
looks like (verified against 2.1.x): a spinner line containing
`esc to interrupt` means *working*; an option list with a `❯ 1.` cursor means
*needs input* (permissions, plan approval, trust prompt, questions); the
`│ >` input box means *awaiting your message*; a dead pane means *exited*
(panes are kept via `remain-on-exit` so you can read the last output and
press `R` to restart). If a future Claude Code changes these strings, status
degrades to `unknown` — nothing breaks; update the markers and the fixtures
in `tests/test_status.py`.

Other contingencies handled deliberately:

- **nvm-installed claude**: the absolute path is resolved at spawn time, so
  tmux's non-interactive shell doesn't need your PATH.
- **Nested tmux**: attaching unsets `$TMUX`, so the dashboard works inside
  your normal tmux session; the inner server's status line shows the
  instance name and the `C-q` hint. Your outer prefix keys stay yours.
- **Corrupt/lost state file**: preserved as `.corrupt` and rebuilt; sessions
  found on the server but missing from the registry are adopted, never
  killed.

## Development

```bash
make test    # unit tests (status heuristics, registry, tmux argv building)
make smoke   # integration test: real tmux + tests/fake_claude.py stand-in
make check   # both
```

The smoke test runs on an isolated socket and data dir and needs no Claude
install or network. See `JOURNAL.md` for the development log and backlog
(color preview, transcript-based status via `~/.claude/projects` JSONL,
git-worktree spawning à la cmux).

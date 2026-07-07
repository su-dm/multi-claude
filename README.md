# multi-claude

**Run a fleet of Claude Code agents from one terminal.**

A tmux-backed dashboard for Linux: a sidebar shows every agent's live status,
context size, cost, and what it's thinking — the selected agent's *real,
fully interactive* session sits right next to it. Type into Claude while
watching the rest of the fleet.

```
 multi-claude                     │ ● Done! All 34 tests pass. Next I'll
 ❯ ◐ 1 ✦backend             87k   │   wire up the retry logic.
     ~/code/backend · working     │
     sonnet-5 · $1.84 · main +3   │ ╭──────────────────────────────────╮
     ∴ the retry loop double-fires│ │ > fix the flaky websocket test█   │
   ● 2 frontend             31k   │ ╰──────────────────────────────────╯
     ~/code/frontend · idle       │   ? for shortcuts
   ◆ 3 infra               156k   │
     ~/code/infra · help          │        ← the actual Claude session,
 ↵ open · n new · ? help          │           not a preview
```

## Key features

- **Live side-by-side** — the pane next to the sidebar *is* the agent's tmux
  pane (perfect fidelity, mouse, scrollback). `Alt-1..9` switches agents from
  anywhere; `Alt-a` jumps to whichever agent needs your input.
- **Three-state status** — working ◐ / idle ● / **help** ◆ (permission
  prompt, plan approval, question), with desktop notifications when an agent
  starts waiting on you.
- **Deep session insight** — per agent: context tokens (yellow at 150k, red
  at 180k), model, session cost, git branch + dirty count, and a one-line
  "current thought" read live from the agent's own transcript.
- **Exact costs (opt-in)** — `multi-claude install-statusline` captures the
  cost figure Claude Code itself computes; without it you get a pricing-table
  estimate marked `~`.
- **Survives everything** — agents live on a dedicated tmux server, not in
  the UI. Dashboard crash: nothing happens. Reboot:
  `multi-claude resume-all` relaunches every agent *continuing its exact
  conversation* (`--resume <session-id>`).
- **Parallel agents on one repo** — spawn agents on isolated **git
  worktrees** (`<repo>.worktrees/<branch>`, browsable side by side); merge
  their branches normally when done.
- **Archive & pin** — `d` hides a finished agent (revivable later with its
  conversation intact), `p` pins the important one to the top.
- **Agent knowledge capture** — `S` asks an agent to condense the session's
  discoveries into a reusable skill; `H` asks it to write `HANDOFF.md` so the
  next session picks up where it left off.
- **Zero heavy deps** — stdlib Python + tmux. No Electron, no pip installs.

## Install

Requires Linux, Python ≥ 3.10, tmux ≥ 3.2, and
[Claude Code](https://docs.anthropic.com/en/docs/claude-code).

```bash
git clone <this repo> && cd multi-claude
./install.sh        # symlinks into ~/.local/bin, prints storage locations
multi-claude        # open the dashboard
```

State lives in `~/.local/share/multi-claude/` (registry, generated tmux
conf, captured costs) plus the dedicated tmux server (`tmux -L
multi-claude`). The installer prints the full list; nothing else is touched.

## Usage

| Keys (anywhere) | |
| --- | --- |
| `Alt-h` / `Alt-l` | focus sidebar / Claude |
| `Alt-1..9` · `Alt-o` · `Alt-a` | switch agent · next · next-needing-input |
| `Alt-z` / `C-q` | zoom pane / detach (everything keeps running) |

| Sidebar | |
| --- | --- |
| `j k g G` / `Enter` | move / show + focus agent |
| `v` | expanded view (model · cost · git · current thought) |
| `n` | new agent — Tab completes dirs; offers worktree isolation in repos |
| `p` / `d` / `A` | pin · archive (revivable) · show archived |
| `N` | toggle notifications (persisted; also `multi-claude notify on\|off`) |
| `R` / `C` | restart fresh / **resume conversation** |
| `i` / `x` / `r` | send one line / kill / rename |
| `c` / `S` / `H` | open claude configs · condense-to-skill · write HANDOFF.md |
| `?` | full key reference |

```bash
multi-claude new ~/code/api -n api            # spawn without the UI
multi-claude new ~/code/api -w agent/fix-auth # spawn on an isolated worktree
multi-claude ls                               # names, status, tokens, dirs
multi-claude send api "run the tests"         # type into an agent
multi-claude resume-all                       # after a reboot: continue all
multi-claude archive api && multi-claude unarchive api
multi-claude install-statusline               # exact cost reporting (opt-in)
multi-claude stats                            # multi-claude's own CPU/RAM cost
```

Configuration via `MULTI_CLAUDE_*` env vars: `SOCKET`, `DATA_DIR`,
`CLAUDE_CMD`, `CLAUDE_HOME`, `POLL_INTERVAL` (default 1s), `NOTIFY` (=0
disables bell/notify-send).

## How it works

Each agent is a **tmux pane** on a dedicated server (your own tmux setup is
never touched). Hidden agents park in a background session; selecting one
`swap-pane`s it next to the sidebar — so there is no preview/attach split and
no embedded terminal emulator, and the UI is stateless by construction.

Status detection is two-layered: marker matching on the visible screen
(spinner → working, `❯ 1.` dialogs → help, input box → idle), with a
screen-change fallback so a future Claude Code UI degrades gracefully instead
of breaking. Tokens, model, cost, and the "current thought" line come from
Claude Code's own transcript files (`~/.claude/projects/…`); exact costs come
from its statusline hook when installed.

Overhead is deliberately tiny: **~40 MiB RAM and <1% of one core** for the
whole dashboard (measured; see `multi-claude stats`), independent of how
heavy the agents themselves are.

## Compatibility

Each release is verified against a specific Claude Code series — see
[CHANGELOG.md](CHANGELOG.md). `multi-claude --version` prints the verified
series, and the dashboard warns if your installed Claude Code differs. Status
heuristics live in one file (`multi_claude/status.py`) with fixtures, so
adapting to a UI change is a small, tested edit.

## Development

```bash
make test    # unit tests (status, registry, transcripts, git, completion)
make smoke   # integration: real tmux + a fake-claude stand-in (no API use)
make check   # both
```

`JOURNAL.md` is the running engineering log (decisions, measured numbers,
tmux gotchas). Contributions should keep `make check` green and update
CHANGELOG.md with the Claude Code series they verified against.

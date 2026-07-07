# multi-claude

<img width="1629" height="543" alt="banner" src="https://github.com/user-attachments/assets/6d6c3c91-bfdf-41e4-969b-1343fb81955b" />

**Manage a fleet of Claude Code agents from one terminal.**

Managing my claude-code sessions was getting annoying.
I wanted a single dashboard where I can switch between them while monitoring the status of my other agents.
This is a minimal tmux-backed claude-code manager with tmux/vim style key-binds.
There's other projects that attempt to do something similar, 
for example [cmux](https://github.com/manaflow-ai/cmux is) really nice but only for MacOS and requires a shell installation.
Multi-Claude works within your existing shell and on Linux. If you have any feature requests let me know.


[multi-claude-demo.webm](https://github.com/user-attachments/assets/ac0cc83b-c512-4c75-b80b-b0b0d99940af)


## Key features

- **Live side-by-side** — the pane next to the sidebar *is* the agent's tmux
  pane. Scroll supported. `Alt-1..9` switches agents from
  anywhere; `Alt-a` jumps to whichever agent needs your input.
- **Three-state status** — working ◐ / idle ● / **help** ◆ (permission
  prompt, plan approval, question), with desktop notifications when an agent
  starts waiting on you.
- **Session metrics** — per agent: cost, context tokens (yellow at 150k, red
  at 180k), model, session cost, git branch + dirty count, and a 
  "current thought" summarizing agent's focus.
- **Revivable sessions** — agents live on a dedicated tmux server, not in
  the UI. Dashboard crash: nothing happens. Reboot and it resumes where you left off.
- **Parallel agents on one repo** — spawn agents on isolated **git
  worktrees** (`<repo>.worktrees/<branch>`, browsable side by side); merge
  their branches normally when done.
- **Archive & pin** — `d` hides a finished agent (revivable later with its
  conversation intact), `p` pins the important one to the top.
- **Agent knowledge capture** — `S` asks an agent to condense the session's
  discoveries into a reusable skill; `H` asks it to write `HANDOFF.md` so the
  next session picks up where it left off.
- **Zero heavy deps** — stdlib Python + tmux. No pip installs.

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
| `C-c` (in sidebar) | quit the dashboard — agents keep running; `multi-claude` reopens |

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
| `<` / `>` | narrow / widen the sidebar (persisted) |
| `?` | full key reference (popup; `j`/`k` scroll, `q` closes) |

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
disables bell/notify-send), `SIDEBAR_WIDTH` (default 34; `<`/`>` adjust it
live).

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

Each release is verified against a specific Claude Code series. `multi-claude --version` prints the verified
series, and the dashboard warns if your installed Claude Code differs. Status
heuristics live in one file (`multi_claude/status.py`) with fixtures, so
adapting to a UI change is a small, tested edit.

## Development

```bash
make test    # unit tests (status, registry, transcripts, git, completion)
make smoke   # integration: real tmux + a fake-claude stand-in (no API use)
make check   # both
```


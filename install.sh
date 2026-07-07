#!/usr/bin/env bash
# Installs multi-claude by symlinking bin/multi-claude into ~/.local/bin.
# The tool is stdlib-only Python, so no pip/venv is needed. For a managed
# install instead, use: pipx install .
set -euo pipefail
cd "$(dirname "$0")"

err() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || err "python3 is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || err "python3 >= 3.10 is required"
command -v tmux >/dev/null || err "tmux is required (sudo apt install tmux)"
command -v claude >/dev/null || \
  printf 'warning: claude not found on PATH; instances will fail to spawn until Claude Code is installed\n' >&2

make install
./bin/multi-claude --version

# Companion skills for the S/H dashboard shortcuts (skip any the user has
# customized — only copy when absent).
SKILLS_DIR="$HOME/.claude/skills"
for skill in condense-to-skill handoff; do
  if [ ! -e "$SKILLS_DIR/$skill" ]; then
    mkdir -p "$SKILLS_DIR"
    cp -r "skills/$skill" "$SKILLS_DIR/"
    printf 'installed skill: %s\n' "$SKILLS_DIR/$skill"
  fi
done

# Exact cost reporting: wire Claude Code's statusline hook into
# ~/.claude/settings.json. Safe by design: an existing statusline keeps
# rendering (chained), and if the hook is ever removed the dashboard just
# falls back to pricing estimates. Revert: multi-claude uninstall-statusline
# Skip with: ./install.sh --no-statusline  (or MULTI_CLAUDE_NO_STATUSLINE=1)
if [ "${1:-}" != "--no-statusline" ] && [ "${MULTI_CLAUDE_NO_STATUSLINE:-0}" != "1" ]; then
  ./bin/multi-claude install-statusline
else
  printf 'skipped statusline hook (exact costs); opt in later: multi-claude install-statusline\n'
fi

DATA_DIR="${MULTI_CLAUDE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/multi-claude}"
cat <<EOF

multi-claude stores its state here:
  $DATA_DIR
    instances.json    instance registry (dirs, launch args, session ids)
    tmux.conf         generated config for the dedicated tmux server
    costs/            per-session costs captured from Claude Code
  tmux server socket: tmux -L multi-claude   (instances live here, not in
                      the dashboard — they survive dashboard restarts)
Also: ~/.claude/skills/{condense-to-skill,handoff} (companion skills for
the S/H shortcuts; only copied if absent), and a statusLine hook in
~/.claude/settings.json for exact cost reporting (any statusline you already
had keeps rendering; revert anytime: multi-claude uninstall-statusline).
Uninstall: multi-claude uninstall-statusline; make uninstall;
rm -rf "$DATA_DIR"; tmux -L multi-claude kill-server

done. run: multi-claude
EOF

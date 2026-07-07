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
printf 'done. run: multi-claude\n'

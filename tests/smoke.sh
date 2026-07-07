#!/usr/bin/env bash
# Integration smoke test: exercises the real tmux backend end-to-end using
# tests/fake_claude.py in place of the claude binary. Needs tmux; does NOT
# need Claude Code or network. Runs on an isolated socket + data dir.
set -euo pipefail
cd "$(dirname "$0")/.."

SOCKET="mc-smoke-$$"
DATA_DIR="$(mktemp -d)"
WORK_DIR="$(mktemp -d)"
export MULTI_CLAUDE_SOCKET="$SOCKET"
export MULTI_CLAUDE_DATA_DIR="$DATA_DIR"
export MULTI_CLAUDE_CLAUDE_CMD="$PWD/tests/fake_claude.py"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

MC() { python3 -m multi_claude "$@"; }

cleanup() {
  tmux -L "$SOCKET" kill-server 2>/dev/null || true
  rm -rf "$DATA_DIR" "$WORK_DIR"
}
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

# Wait until `multi-claude ls` reports the given status for an instance.
wait_status() {
  local name="$1" want="$2" deadline=$((SECONDS + 15))
  while ((SECONDS < deadline)); do
    if MC ls | grep -F "$name" | grep -qF "$want"; then return 0; fi
    sleep 0.5
  done
  echo "--- last ls output ---" >&2; MC ls >&2 || true
  echo "--- pane capture ---" >&2; tmux -L "$SOCKET" capture-pane -p -t "=$name:" >&2 || true
  fail "instance $name never reached status '$want'"
}

echo "1. create instance"
MC new "$WORK_DIR" -n smoke | grep -q "created smoke" || fail "create"

echo "2. status transitions busy -> awaiting message"
wait_status smoke "working"
wait_status smoke "awaiting message"

echo "3. send text without attaching"
MC send smoke "hello from smoke test"
wait_status smoke "working"
wait_status smoke "awaiting message"
tmux -L "$SOCKET" capture-pane -p -t "=smoke:" | grep -q "you said: hello from smoke test" \
  || fail "sent text was not echoed"

echo "4. exited detection (remain-on-exit keeps the dead pane)"
MC send smoke "exit"
wait_status smoke "exited"

echo "5. kill removes instance"
MC kill smoke
MC ls | grep -qF "no instances" || fail "instance still listed after kill"

echo "SMOKE PASS"

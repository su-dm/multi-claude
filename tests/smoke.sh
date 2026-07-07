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
T()  { tmux -L "$SOCKET" "$@"; }

cleanup() {
  T kill-server 2>/dev/null || true
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
  fail "instance $name never reached status '$want'"
}

viewer_pane_cmd() {
  # start command of the non-sidebar pane in the dashboard window
  T list-panes -t mc-dash:dash -F '#{pane_start_command}' | grep -v "multi_claude sidebar"
}

echo "0. v0.1 legacy session is migrated with its registry entry re-pointed"
# Emulate v0.1 state: instance as its own session (window named "claude",
# like a real v0.1 spawn) + a registry entry without pane_id.
T new-session -d -s legacy -n claude -x 80 -y 24 "$PWD/tests/fake_claude.py 600"
cat > "$DATA_DIR/instances.json" <<EOF
{"instances": [{"name": "legacy", "cwd": "$WORK_DIR", "command": ["$PWD/tests/fake_claude.py"], "created_at": 0}]}
EOF

echo "1. bootstrap creates dashboard (sidebar + welcome panes)"
MC bootstrap >/dev/null
T list-sessions -F '#{session_name}' | grep -qx "legacy" && fail "legacy session not migrated"
MC ls | grep -F "legacy" | grep -qF "$WORK_DIR" || fail "legacy registry entry lost its metadata"
python3 -c "
import json, sys
data = json.load(open('$DATA_DIR/instances.json'))
insts = data['instances']
assert len(insts) == 1, f'expected 1 instance after migration, got {[i[\"name\"] for i in insts]}'
assert insts[0]['name'] == 'legacy' and insts[0]['pane_id'], insts
" || fail "legacy instance not re-pointed to its pane"
MC kill legacy
T list-sessions -F '#{session_name}' | grep -qx "mc-dash" || fail "no mc-dash session"
panes=$(T list-panes -t mc-dash:dash -F '#{pane_start_command}')
grep -q "multi_claude sidebar" <<<"$panes" || fail "sidebar pane missing"
grep -q "multi_claude welcome" <<<"$panes" || fail "welcome pane missing"

echo "2. create instance; status working -> idle"
MC new "$WORK_DIR" -n smoke | grep -q "created smoke" || fail "create"
wait_status smoke "working"
wait_status smoke "idle"

echo "3. select swaps the instance pane into the dashboard"
MC select smoke
viewer_pane_cmd | grep -v multi_claude | grep -q "fake_claude" \
  || fail "instance pane not in viewer slot"
MC ls | grep -F "smoke" | grep -q '\*' || fail "ls does not mark smoke as displayed"

echo "4. send text; screen-change detection sees work; echo lands"
MC send smoke "hello from smoke test"
wait_status smoke "working"
wait_status smoke "idle"
# exclude sidebar/welcome panes, whose env pins CLAUDE_CMD=...fake_claude.py
pane=$(T list-panes -a -F '#{pane_id} #{pane_start_command}' \
  | grep fake_claude | grep -v multi_claude | head -1 | cut -d' ' -f1)
T capture-pane -p -t "$pane" | grep -q "you said: hello from smoke test" \
  || fail "sent text was not echoed"

echo "5. second instance; select 2 / select next route correctly"
MC new "$WORK_DIR" -n smoke2 >/dev/null
wait_status smoke2 "idle"
MC select 2
MC ls | grep -F "smoke2" | grep -q '\*' || fail "select 2 did not display smoke2"
MC select next
MC ls | grep -F "smoke " | grep -q '\*' || fail "select next did not cycle back to smoke"

echo "6. exited detection (remain-on-exit keeps the dead pane)"
MC send smoke "exit"
wait_status smoke "exited"

echo "7. killing the displayed instance returns the welcome pane"
MC kill smoke
viewer_pane_cmd | grep -q "multi_claude welcome" || fail "welcome pane not restored"
MC kill smoke2
MC ls | grep -qF "no instances" || fail "instances still listed after kill"

echo "SMOKE PASS"

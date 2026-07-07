"""multi-claude: a terminal dashboard for managing multiple Claude Code instances.

Architecture: each instance is a tmux pane on a dedicated server (socket
"multi-claude"); the dashboard is a tmux window pairing a curses sidebar with
the selected instance's real pane (swap-pane). Status comes from visible-
screen heuristics + change detection; token usage from Claude Code's
transcript files. Instances survive dashboard restarts — tmux owns them.
"""

__version__ = "0.5.1"

# The Claude Code release series this version's status heuristics and
# transcript parsing were verified against (see CHANGELOG.md). A different
# series still runs — unknown UI degrades to the screen-change fallback —
# but the dashboard shows a warning.
CLAUDE_CODE_VERIFIED = "2.1"

"""multi-claude: a terminal dashboard for managing multiple Claude Code instances.

Architecture: each instance runs in a detached tmux session on a dedicated
tmux server (socket name "multi-claude"). The curses dashboard polls pane
content for status heuristics and previews; attaching hands the real terminal
to tmux, so rendering fidelity is exact and instances survive dashboard
restarts.
"""

__version__ = "0.2.0"

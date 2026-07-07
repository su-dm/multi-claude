"""Git branch/dirty status + worktree helpers for instance directories.

Status reads are cached per directory with a TTL so the 1 s poll loop
doesn't fork `git status` on every tick for every instance (git status on a
large repo can take hundreds of ms).
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitStatus:
    branch: str
    dirty: int          # modified/untracked entries in `status --porcelain`
    is_worktree: bool   # a linked worktree (not the main checkout)

    def summary(self) -> str:
        mark = "±" if self.is_worktree else ""
        dirty = f" +{self.dirty}" if self.dirty else ""
        return f"{mark}{self.branch}{dirty}"


def _git(cwd: str, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout
    )


def is_git_repo(cwd: str) -> bool:
    try:
        proc = _git(cwd, "rev-parse", "--is-inside-work-tree", timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def read_status(cwd: str) -> GitStatus | None:
    try:
        proc = _git(cwd, "status", "--porcelain", "--branch")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.splitlines()
    branch = "?"
    if lines and lines[0].startswith("## "):
        branch = lines[0][3:].split("...")[0].strip()
        if branch.startswith("No commits yet on "):
            branch = lines[0][3:].removeprefix("No commits yet on ").strip()
    dirty = sum(1 for ln in lines[1:] if ln.strip())
    # Linked worktrees have a .git *file* (pointing at the main repo's
    # worktrees dir) instead of a .git directory.
    try:
        git_common = _git(cwd, "rev-parse", "--git-dir", "--git-common-dir", timeout=2)
        git_dir, common_dir = (git_common.stdout.splitlines() + ["", ""])[:2]
        is_worktree = bool(git_dir) and git_dir != common_dir
    except (OSError, subprocess.TimeoutExpired):
        is_worktree = False
    return GitStatus(branch=branch, dirty=dirty, is_worktree=is_worktree)


class GitStatusCache:
    def __init__(self, ttl: float = 10.0):
        self.ttl = ttl
        self._cache: dict[str, tuple[float, GitStatus | None]] = {}

    def status(self, cwd: str) -> GitStatus | None:
        now = time.monotonic()
        hit = self._cache.get(cwd)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        result = read_status(cwd)
        self._cache[cwd] = (now, result)
        return result


# -- worktree creation --------------------------------------------------------

def worktree_root(repo_dir: str) -> str:
    """Sibling directory holding this repo's multi-claude worktrees:
    ~/code/proj -> ~/code/proj.worktrees/ (plain directories, so the user can
    browse every workspace side by side). Symlinks are resolved so the result
    is comparable with git's own (resolved) --show-toplevel output — on macOS
    /tmp and /var are symlinks into /private."""
    return os.path.realpath(repo_dir).rstrip("/") + ".worktrees"


def repo_identity(cwd: str) -> str | None:
    """A key identifying the underlying repository: the absolute git common
    dir. Worktrees of one repo share it, so agents on different worktrees of
    the same project can be recognized as siblings."""
    try:
        proc = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def repo_toplevel(cwd: str) -> str | None:
    try:
        proc = _git(cwd, "rev-parse", "--show-toplevel", timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def create_worktree(repo_dir: str, branch: str) -> str:
    """Create (or reuse) a worktree for `branch`; returns its directory.

    New branches fork from the repo's current HEAD. If the branch already
    exists it is checked out as-is; if its worktree directory already exists
    it is reused (so re-spawning an agent on the same branch just works).
    """
    top = repo_toplevel(repo_dir)
    if top is None:
        raise GitError(f"not a git repository: {repo_dir}")
    path = os.path.join(worktree_root(top), branch.replace("/", "-"))
    if os.path.isdir(path):
        if is_git_repo(path):
            return path
        raise GitError(f"{path} exists but is not a git worktree")
    branch_exists = _git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    if branch_exists:
        args = ["worktree", "add", path, branch]
    else:
        args = ["worktree", "add", "-b", branch, path]
    proc = _git(top, *args, timeout=60)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git worktree add failed for {branch}")
    return path

"""Git status + worktree tests against real throwaway repos."""

import os
import subprocess
import tempfile
import unittest

from multi_claude.gitinfo import (
    GitError,
    create_worktree,
    is_git_repo,
    read_status,
    worktree_root,
)


def git(cwd, *args):
    subprocess.run(
        ["git", "-C", cwd, *args], check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


class GitInfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.repo)
        git(self.repo, "init", "-b", "main")
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("hello\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_git_repo(self):
        self.assertTrue(is_git_repo(self.repo))
        self.assertFalse(is_git_repo(self.tmp.name))

    def test_status_clean_and_dirty(self):
        st = read_status(self.repo)
        self.assertEqual((st.branch, st.dirty, st.is_worktree), ("main", 0, False))
        with open(os.path.join(self.repo, "b.txt"), "w") as fh:
            fh.write("x")
        st = read_status(self.repo)
        self.assertEqual(st.dirty, 1)
        self.assertIn("+1", st.summary())

    def test_worktree_create_new_branch(self):
        path = create_worktree(self.repo, "agent/feature-x")
        self.assertTrue(path.startswith(worktree_root(self.repo)))
        self.assertTrue(os.path.isfile(os.path.join(path, "a.txt")))
        st = read_status(path)
        self.assertEqual(st.branch, "agent/feature-x")
        self.assertTrue(st.is_worktree)
        # Main checkout is untouched and still browsable next to the worktree.
        self.assertEqual(read_status(self.repo).branch, "main")

    def test_worktree_reuse_existing(self):
        path1 = create_worktree(self.repo, "agent/x")
        path2 = create_worktree(self.repo, "agent/x")
        self.assertEqual(path1, path2)

    def test_worktree_from_subdirectory_uses_toplevel(self):
        sub = os.path.join(self.repo, "src")
        os.makedirs(sub)
        path = create_worktree(sub, "agent/y")
        self.assertTrue(path.startswith(worktree_root(self.repo)))

    def test_worktree_existing_branch(self):
        git(self.repo, "branch", "feature-old")
        path = create_worktree(self.repo, "feature-old")
        self.assertEqual(read_status(path).branch, "feature-old")

    def test_worktree_non_repo_raises(self):
        with self.assertRaises(GitError):
            create_worktree(self.tmp.name, "x")


if __name__ == "__main__":
    unittest.main()

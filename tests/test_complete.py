"""Directory tab-completion tests (multi_claude.ui.complete_dir)."""

import os
import tempfile
import unittest

from multi_claude.ui import complete_dir


class CompleteDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for d in ("projects", "pictures", "music", ".hidden"):
            os.mkdir(os.path.join(self.root, d))
        open(os.path.join(self.root, "profile.txt"), "w").close()  # file: never offered

    def tearDown(self):
        self.tmp.cleanup()

    def test_unique_match_completes_with_slash(self):
        text, cands = complete_dir(os.path.join(self.root, "mu"))
        self.assertEqual(text, os.path.join(self.root, "music") + "/")
        self.assertEqual(cands, ["music"])

    def test_common_prefix_of_multiple_matches(self):
        text, cands = complete_dir(os.path.join(self.root, "p"))
        self.assertEqual(text, os.path.join(self.root, "p"))  # common prefix is just "p"
        self.assertEqual(cands, ["pictures", "projects"])

    def test_trailing_slash_lists_children(self):
        _, cands = complete_dir(self.root + "/")
        self.assertEqual(cands, ["music", "pictures", "projects"])

    def test_files_are_not_offered(self):
        _, cands = complete_dir(os.path.join(self.root, "pro"))
        self.assertEqual(cands, ["projects"])

    def test_hidden_dirs_only_with_dot_fragment(self):
        _, cands = complete_dir(self.root + "/")
        self.assertNotIn(".hidden", cands)
        text, cands = complete_dir(os.path.join(self.root, ".h"))
        self.assertEqual(cands, [".hidden"])
        self.assertTrue(text.endswith(".hidden/"))

    def test_no_match_returns_input(self):
        text, cands = complete_dir(os.path.join(self.root, "zzz"))
        self.assertEqual(text, os.path.join(self.root, "zzz"))
        self.assertEqual(cands, [])

    def test_nonexistent_base_returns_input(self):
        text, cands = complete_dir("/definitely/not/a/dir/x")
        self.assertEqual(cands, [])
        self.assertEqual(text, "/definitely/not/a/dir/x")

    def test_tilde_is_preserved(self):
        home = os.path.expanduser("~")
        subdirs = sorted(
            e for e in os.listdir(home)
            if os.path.isdir(os.path.join(home, e)) and not e.startswith(".")
        )
        if not subdirs:
            self.skipTest("no visible directories in $HOME")
        frag = subdirs[0]
        text, _ = complete_dir("~/" + frag)
        self.assertTrue(text.startswith("~/"), text)


if __name__ == "__main__":
    unittest.main()

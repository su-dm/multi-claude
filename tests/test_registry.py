import json
import tempfile
import unittest
from pathlib import Path

from multi_claude.registry import Instance, Registry, sanitize_name


class SanitizeNameTest(unittest.TestCase):
    def test_replaces_tmux_hostile_chars(self):
        self.assertEqual(sanitize_name("my proj: v2.0"), "my-proj-v2-0")

    def test_empty_falls_back(self):
        self.assertEqual(sanitize_name("  ::"), "claude")


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "instances.json"

    def tearDown(self):
        self.dir.cleanup()

    def make(self) -> Registry:
        return Registry(self.path)

    def test_roundtrip(self):
        reg = self.make()
        reg.add(Instance(name="alpha", cwd="/tmp", command=["claude"]))
        reg2 = self.make()
        inst = reg2.get("alpha")
        self.assertIsNotNone(inst)
        self.assertEqual(inst.cwd, "/tmp")
        self.assertEqual(inst.command, ["claude"])

    def test_duplicate_add_rejected(self):
        reg = self.make()
        reg.add(Instance(name="a", cwd="/tmp"))
        with self.assertRaises(ValueError):
            reg.add(Instance(name="a", cwd="/tmp"))

    def test_unique_name_suffixes(self):
        reg = self.make()
        reg.add(Instance(name="proj", cwd="/tmp"))
        reg.add(Instance(name="proj-2", cwd="/tmp"))
        self.assertEqual(reg.unique_name("proj"), "proj-3")

    def test_remove_and_rename(self):
        reg = self.make()
        reg.add(Instance(name="a", cwd="/tmp"))
        reg.rename("a", "b")
        self.assertIsNone(reg.get("a"))
        self.assertIsNotNone(reg.get("b"))
        reg.remove("b")
        self.assertEqual(self.make().instances, [])

    def test_corrupt_file_recovers(self):
        self.path.write_text("{not json")
        reg = self.make()
        self.assertEqual(reg.instances, [])
        self.assertTrue(self.path.with_suffix(".json.corrupt").exists())
        reg.add(Instance(name="a", cwd="/tmp"))  # and it can save again
        self.assertEqual(json.loads(self.path.read_text())["instances"][0]["name"], "a")

    def test_adopt_unknown_sessions(self):
        reg = self.make()
        reg.add(Instance(name="known", cwd="/tmp"))
        adopted = reg.adopt_unknown_sessions(["known", "stray"])
        self.assertEqual([i.name for i in adopted], ["stray"])
        self.assertIsNotNone(self.make().get("stray"))


if __name__ == "__main__":
    unittest.main()

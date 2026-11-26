import tempfile
import unittest
from pathlib import Path
from tankai.web.skill_store import catalog, change

class SkillStoreTest(unittest.TestCase):
    def test_import_enable_disable_persistence_and_isolation(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            root = Path(a)
            self.assertEqual(len(catalog(root, "alice")), 4)
            self.assertTrue(all(not s["imported"] and not s["execution_available"] for s in catalog(root, "alice")))
            with self.assertRaises(ValueError):
                change(root, "alice", "rag-eval", "enable")
            change(root, "alice", "rag-eval", "import")
            change(root, "alice", "rag-eval", "enable")
            state = {s["id"]: s for s in catalog(root, "alice")}["rag-eval"]
            self.assertTrue(state["enabled"])
            self.assertTrue(state["guidance"])
            change(root, "alice", "rag-eval", "import")
            self.assertTrue({s["id"]: s for s in catalog(root, "alice")}["rag-eval"]["enabled"])
            self.assertFalse(any(s["imported"] for s in catalog(root, "bob")))
            self.assertFalse(any(s["imported"] for s in catalog(Path(b), "alice")))
            change(root, "alice", "rag-eval", "disable")
            self.assertIsNone({s["id"]: s for s in catalog(root, "alice")}["rag-eval"]["guidance"])
            for key, action in [("../evil", "import"), ("rag-eval", "execute")]:
                with self.assertRaises(ValueError):
                    change(root, "alice", key, action)

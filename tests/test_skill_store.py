import tempfile
import unittest
from pathlib import Path
from tankai.web.skill_store import catalog, change, document

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

    def test_pinned_documents_are_read_only_and_require_import(self):
        with tempfile.TemporaryDirectory() as a:
            root = Path(a)
            for skill in catalog(root, "alice"):
                key = skill["id"]
                with self.assertRaises(PermissionError):
                    document(root, "alice", key)
                change(root, "alice", key, "import")
                doc = document(root, "alice", key)
                self.assertIn("name: " + key, doc["content"])
                self.assertEqual(doc["revision"], skill["revision"])
                self.assertFalse(doc["execution_available"])
                with self.assertRaises(PermissionError):
                    document(root, "bob", key)
            with self.assertRaises(ValueError):
                document(root, "alice", "../LICENSE")

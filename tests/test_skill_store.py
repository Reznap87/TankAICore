import tempfile
import unittest
from pathlib import Path
from tankai.web.skill_store import catalog, change, document, run_local

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

    def test_local_adapter(self):
        with tempfile.TemporaryDirectory() as a:
            root = Path(a)
            payload = {"ranked_ids": ["x", "a"], "relevant_ids": ["a", "b"]}
            with self.assertRaises(PermissionError):
                run_local(root, "alice", "rag-eval", payload)
            change(root, "alice", "rag-eval", "import")
            change(root, "alice", "rag-eval", "enable")
            result = run_local(root, "alice", "rag-eval", payload)
            self.assertEqual((result["precision"], result["recall"], result["reciprocal_rank"]), (.5, .5, .5))
            self.assertEqual(result["external_calls"], 0)
            self.assertFalse(result["nvidia_execution"])
            with self.assertRaises(PermissionError):
                run_local(root, "bob", "rag-eval", payload)
            with self.assertRaises(ValueError):
                run_local(root, "alice", "rag-eval", {"ranked_ids": ["a", "a"], "relevant_ids": ["a"]})
            change(root, "alice", "rag-eval", "disable")
            with self.assertRaises(PermissionError):
                run_local(root, "alice", "rag-eval", payload)

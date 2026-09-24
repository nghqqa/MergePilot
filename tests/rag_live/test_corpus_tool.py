"""corpus_tool 单元测试(第 1 层):校验 / 快照确定性 / 幂等导入。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest

_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_LIVE_CORPUS = os.path.join(_REPO, "tools", "rag", "corpus",
                            "org-security-knowledge-v1.json")


def _load():
    p = os.path.join(_REPO, "tools", "rag", "corpus_tool.py")
    spec = importlib.util.spec_from_file_location("corpus_tool_ut", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ct = _load()


def _minimal():
    return {"data_mode": "SYNTHETIC", "retrieval_mode": "lexical-zh-en-v1",
            "strategy_id": "test-v1",
            "documents": [{"document_id": "d1", "title": "t",
                           "chunks": [{"chunk_id": "d1#1", "text": "hello",
                                       "source_ref": "x.md#1"}]}]}


class ValidateTests(unittest.TestCase):
    def test_repo_corpus_is_valid(self):
        ct.validate(ct.load(_LIVE_CORPUS))  # 不抛即通过

    def test_missing_top_field(self):
        d = _minimal()
        del d["data_mode"]
        with self.assertRaises(ValueError):
            ct.validate(d)

    def test_duplicate_chunk_id(self):
        d = _minimal()
        d["documents"][0]["chunks"].append(dict(d["documents"][0]["chunks"][0]))
        with self.assertRaises(ValueError):
            ct.validate(d)

    def test_empty_chunk_text(self):
        d = _minimal()
        d["documents"][0]["chunks"][0]["text"] = ""
        with self.assertRaises(ValueError):
            ct.validate(d)


class SnapshotTests(unittest.TestCase):
    def test_deterministic_and_formatting_insensitive(self):
        a = ct.snapshot_id(_minimal())
        reformatted = json.loads(json.dumps(_minimal(), indent=4))
        self.assertEqual(a, ct.snapshot_id(reformatted))

    def test_content_change_changes_id(self):
        d = _minimal()
        a = ct.snapshot_id(d)
        d["documents"][0]["chunks"][0]["text"] = "changed"
        self.assertNotEqual(a, ct.snapshot_id(d))


class ImportTests(unittest.TestCase):
    def test_import_idempotent_no_unbounded_growth(self):
        """相同资料重复导入:第二次 no-op,不产生重复记录。"""
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "src.json")
            dst = os.path.join(t, "dst.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(_minimal(), f)
            r1 = ct.import_corpus(src, dst)
            r2 = ct.import_corpus(src, dst)
            self.assertTrue(r1["changed"])
            self.assertFalse(r2["changed"])
            self.assertEqual(r1["snapshot_id"], r2["snapshot_id"])

    def test_import_rejects_invalid_corpus(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "bad.json")
            dst = os.path.join(t, "dst.json")
            d = _minimal()
            del d["strategy_id"]
            with open(src, "w", encoding="utf-8") as f:
                json.dump(d, f)
            with self.assertRaises(ValueError):
                ct.import_corpus(src, dst)
            self.assertFalse(os.path.exists(dst))  # 坏语料不落盘

    def test_import_update_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "src.json")
            dst = os.path.join(t, "dst.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(_minimal(), f)
            ct.import_corpus(src, dst)
            old = ct.load(dst)
            d = _minimal()
            d["documents"][0]["chunks"].append(
                {"chunk_id": "d1#2", "text": "new", "source_ref": "x.md#2"})
            with open(src, "w", encoding="utf-8") as f:
                json.dump(d, f)
            r = ct.import_corpus(src, dst)
            self.assertTrue(r["changed"])
            new = ct.load(dst)
            self.assertEqual(len(new["documents"][0]["chunks"]), 2)
            self.assertNotEqual(ct.snapshot_id(old), ct.snapshot_id(new))
            self.assertFalse(os.path.exists(dst + ".tmp-import"))  # 无残留临时文件


if __name__ == "__main__":
    unittest.main(verbosity=2)

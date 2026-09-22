"""RAG 语料事实源工具:校验、快照标识、幂等导入。

快照标识(snapshot_id)=语料规范化 JSON 的 sha256(内容寻址,不手工起版本号):
内容不变 → 同一 snapshot;任何内容变化(增删 chunk、改文字)→ 新 snapshot。
这使 run-manifest 能把"实际加载的知识"绑定到具体字节,而不是版本名字符串。

用法:
  python corpus_tool.py snapshot <corpus.json>
  python corpus_tool.py import <src.json> <dst.json>   # 内容相同则无操作
"""
from __future__ import annotations

import hashlib
import json
import os
import sys


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_id(doc: dict) -> str:
    """内容寻址快照 ID(规范化 sha256,与文件排版无关)。"""
    return hashlib.sha256(_canon(doc).encode("utf-8")).hexdigest()


def validate(doc: dict) -> None:
    """违反语料契约即 ValueError(编程/数据错误,非运行时状态)。"""
    for key in ("data_mode", "retrieval_mode", "strategy_id", "documents"):
        if key not in doc:
            raise ValueError("语料缺少字段: %s" % key)
    if not isinstance(doc["documents"], list) or not doc["documents"]:
        raise ValueError("documents 必须非空数组")
    seen_docs, seen_chunks = set(), set()
    for d in doc["documents"]:
        for key in ("document_id", "title", "chunks"):
            if key not in d:
                raise ValueError("文档缺字段: %s" % key)
        if d["document_id"] in seen_docs:
            raise ValueError("document_id 重复: %s" % d["document_id"])
        seen_docs.add(d["document_id"])
        if not isinstance(d["chunks"], list) or not d["chunks"]:
            raise ValueError("chunks 必须非空: %s" % d["document_id"])
        for ch in d["chunks"]:
            for key in ("chunk_id", "text", "source_ref"):
                if key not in ch or not str(ch.get(key, "")).strip():
                    raise ValueError("chunk 缺字段: %s.%s" % (d["document_id"], key))
            if ch["chunk_id"] in seen_chunks:
                raise ValueError("chunk_id 重复: %s" % ch["chunk_id"])
            seen_chunks.add(ch["chunk_id"])


def _chunk_count(doc: dict) -> int:
    return sum(len(d["chunks"]) for d in doc["documents"])


def import_corpus(src_path: str, dst_path: str) -> dict:
    """幂等导入:目标已存在且快照相同 → no-op;否则原子替换(tmp+rename)。

    重复导入不会无界产生重复记录(单文件快照制,天然去重);
    知识更新 = 新快照整体替换,旧快照由 run-manifest 中的历史 ID 追溯。
    """
    src = load(src_path)
    validate(src)
    sid = snapshot_id(src)
    if os.path.exists(dst_path):
        try:
            dst = load(dst_path)
            if snapshot_id(dst) == sid:
                return {"changed": False, "snapshot_id": sid,
                        "chunks": _chunk_count(src)}
        except Exception:
            pass  # 目标损坏 → 用新快照覆盖(原子替换)
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)) or ".", exist_ok=True)
    tmp = dst_path + ".tmp-import"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(src, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, dst_path)
    return {"changed": True, "snapshot_id": sid, "chunks": _chunk_count(src)}


def describe(doc: dict) -> dict:
    """manifest/审计用的语料摘要(不含正文,无秘密面)。"""
    return {"snapshot_id": snapshot_id(doc),
            "chunks": _chunk_count(doc),
            "documents": len(doc["documents"]),
            "data_mode": doc["data_mode"],
            "retrieval_mode": doc["retrieval_mode"],
            "strategy_id": doc["strategy_id"]}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "snapshot":
        print(snapshot_id(load(sys.argv[2])))
    elif len(sys.argv) >= 4 and sys.argv[1] == "import":
        print(json.dumps(import_corpus(sys.argv[2], sys.argv[3]), ensure_ascii=False))
    else:
        print(__doc__)
        sys.exit(2)

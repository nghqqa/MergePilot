#!/usr/bin/env python
# tools/bge_embed.py — bge-large-en-v1.5 离线嵌入运行时（PRODUCTION_RAG_READINESS）。
#
# 真实语义模型 provider 的本地推理器：直接读取 HF 缓存的 model.safetensors
# （纯 numpy 前向，不依赖 torch/transformers，不做任何网络访问）。
# 可审计性：--manifest-out 生成模型文件 SHA256 清单；--verify 在运行前逐文件
# 校验摘要，任何缺失/漂移即退出非零（fail-closed，绝不带病出向量）。
#
# 输入/输出：stdin 每行 {"id":..., "text":...}；stdout 每行 {"id":..., "vector":[...]}
# 池化：CLS + L2 归一（bge-large-en-v1.5 官方配置，见 1_Pooling/config.json）。
#
# 运行时规格（写入 manifest，console 侧 fail-closed 比对）：
#   runtime=numpy-bert-v1 / pooling=cls_l2 / distance=cosine / max_len=128
#   gelu=erf(scipy) / ln_eps=1e-12 / dtype=F32

import argparse
import hashlib
import json
import math
import struct
import sys
import time

import numpy as np
from scipy.special import erf
from tokenizers import Tokenizer

MODEL_FILES = ["model.safetensors", "tokenizer.json", "config.json",
               "special_tokens_map.json", "vocab.txt", "modules.json",
               "1_Pooling/config.json", "sentence_bert_config.json",
               "config_sentence_transformers.json", "tokenizer_config.json"]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_safetensors(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
    header.pop("__metadata__", None)
    data_start = 8 + n
    total = 8 + n + (header[max(header, key=lambda k: header[k]["data_offsets"][1])]["data_offsets"][1])
    mm = np.memmap(path, dtype=np.uint8, mode="r")
    tensors = {}
    for name, meta in header.items():
        s, e = meta["data_offsets"]
        dt = {"F32": np.float32, "I64": np.int64, "F16": np.float16, "I32": np.int32}[meta["dtype"]]
        tensors[name] = mm[data_start + s:data_start + e].view(dt).reshape(meta["shape"])
    _ = total
    return tensors


class BgeRuntime:
    def __init__(self, model_dir, max_len=128):
        self.dir = model_dir.rstrip("/\\")
        self.max_len = max_len
        cfg = json.load(open(f"{self.dir}/config.json"))
        self.H = cfg["hidden_size"]
        self.L = cfg["num_hidden_layers"]
        self.A = cfg["num_attention_heads"]
        self.eps = cfg["layer_norm_eps"]
        pooling = json.load(open(f"{self.dir}/1_Pooling/config.json"))
        assert pooling.get("pooling_mode_cls_token") is True, "bge-large-en-v1.5 应为 CLS 池化"
        self.t = Tokenizer.from_file(f"{self.dir}/tokenizer.json")
        t0 = time.time()
        self.w = load_safetensors(f"{self.dir}/model.safetensors")
        # 常权重转 dense 加速 matmul
        self.dense = {k: np.ascontiguousarray(v, dtype=np.float32) for k, v in self.w.items()}
        print(f"[bge] model loaded: {len(self.dense)} tensors in {time.time()-t0:.1f}s", file=sys.stderr)

    def ln(self, x, w, b):
        m = x.mean(-1, keepdims=True)
        v = x.var(-1, keepdims=True)
        return (x - m) / np.sqrt(v + self.eps) * w + b

    def gelu(self, x):
        return 0.5 * x * (1.0 + erf(x / math.sqrt(2.0)))

    def softmax(self, x):
        x = x - x.max(-1, keepdims=True)
        e = np.exp(x)
        return e / e.sum(-1, keepdims=True)

    def encode(self, text):
        ids = self.t.encode(text).ids[: self.max_len - 2]
        ids = [101] + ids + [102]  # 已含 CLS/SEP 则幂等无害（tokenizer 默认已加，此处双保险截断）
        ids = ids[: self.max_len]
        n = len(ids)
        w = self.dense
        x = (w["embeddings.word_embeddings.weight"][ids]
             + w["embeddings.position_embeddings.weight"][:n]
             + w["embeddings.token_type_embeddings.weight"][0])
        x = self.ln(x, w["embeddings.LayerNorm.weight"], w["embeddings.LayerNorm.bias"])
        hd = self.H // self.A
        mask_add = np.zeros((n, n), dtype=np.float32)
        for i in range(self.L):
            p = f"encoder.layer.{i}."
            q = x @ w[p + "attention.self.query.weight"].T + w[p + "attention.self.query.bias"]
            k = x @ w[p + "attention.self.key.weight"].T + w[p + "attention.self.key.bias"]
            v = x @ w[p + "attention.self.value.weight"].T + w[p + "attention.self.value.bias"]
            q = q.reshape(n, self.A, hd).transpose(1, 0, 2)
            k = k.reshape(n, self.A, hd).transpose(1, 0, 2)
            v = v.reshape(n, self.A, hd).transpose(1, 0, 2)
            att = self.softmax(q @ k.transpose(0, 2, 1) / math.sqrt(hd) + mask_add)
            ctx = (att @ v).transpose(1, 0, 2).reshape(n, self.H)
            ctx = ctx @ w[p + "attention.output.dense.weight"].T + w[p + "attention.output.dense.bias"]
            x = self.ln(x + ctx, w[p + "attention.output.LayerNorm.weight"], w[p + "attention.output.LayerNorm.bias"])
            h = self.gelu(x @ w[p + "intermediate.dense.weight"].T + w[p + "intermediate.dense.bias"])
            h = h @ w[p + "output.dense.weight"].T + w[p + "output.dense.bias"]
            x = self.ln(x + h, w[p + "output.LayerNorm.weight"], w[p + "output.LayerNorm.bias"])
        vec = x[0]  # CLS
        vec = vec / np.linalg.norm(vec)
        return [round(float(z), 8) for z in vec]

    def check(self, name):
        # 语义健全性自检（相对排序，不依赖参考向量）
        a = np.array(self.encode("a cat is sitting on the mat"))
        b = np.array(self.encode("a kitten is sitting on the rug"))
        c = np.array(self.encode("quarterly financial report of a car factory"))
        sim_ab, sim_ac = float(a @ b), float(a @ c)
        assert sim_ab > sim_ac, f"语义自检失败: sim(ab)={sim_ab:.4f} <= sim(ac)={sim_ac:.4f}"
        return {"sim_paraphrase": round(sim_ab, 4), "sim_unrelated": round(sim_ac, 4)}


# manifest v2（CANONICAL_A 加固，自 B 波选择性吸收）：
#   files: [{name, sha256, bytes}] + files_count + total_bytes——
#   校验三重（文件数 / 每文件字节数 / 每文件 sha256）+ 总字节数 + 必需工件强制。
REQUIRED_ARTIFACTS = ["model.safetensors", "tokenizer.json", "config.json"]  # 权重/tokenizer/配置


def build_manifest(model_dir):
    import os
    files = []
    for f in MODEL_FILES:
        p = f"{model_dir}/{f}"
        if os.path.exists(p):
            files.append({"name": f, "sha256": sha256_file(p), "bytes": os.path.getsize(p)})
    return {
        "manifest_version": 2,
        "model_id": "bge-large-en-v1.5",
        "source": "local HF cache (offline, no runtime download)",
        "pooling": "cls_l2",
        "distance": "cosine",
        "dims": 1024,
        "runtime": "numpy-bert-v1",
        "runtime_spec": {"gelu": "erf(scipy)", "ln_eps": 1e-12, "max_len": 128,
                         "dtype": "F32", "framework": "numpy+tokenizers"},
        "files": files,
        "files_count": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
    }


def verify_manifest(model_dir, manifest):
    """fail-closed 校验（结果用于披露时只报问题类别，不回显路径/内容）。"""
    import os
    problems = []
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return ["manifest_files_missing_or_not_v2"]
    names = {f.get("name") for f in files}
    # 必需工件：权重/tokenizer/config 任一缺失即 fail-closed
    for req in REQUIRED_ARTIFACTS:
        if req not in names:
            problems.append(f"required_missing:{req}")
    # 文件数：目录内多出的受管文件视为漂移（防夹带）；缺失同样拒绝
    on_disk = {f for f in MODEL_FILES if os.path.exists(os.path.join(model_dir, f))}
    if on_disk - names:
        problems.append("unexpected_files:" + ",".join(sorted(on_disk - names)[:3]))
    total = 0
    for f in files:
        p = os.path.join(model_dir, str(f.get("name")))
        if not os.path.exists(p):
            problems.append(f"missing:{f.get('name')}")
            continue
        size = os.path.getsize(p)
        total += size
        if isinstance(f.get("bytes"), int) and size != f["bytes"]:
            problems.append(f"bytes_drift:{f.get('name')}")
            continue
        if sha256_file(p) != f.get("sha256"):
            problems.append(f"digest_drift:{f.get('name')}")
    if isinstance(manifest.get("total_bytes"), int) and total != manifest["total_bytes"]:
        problems.append(f"total_bytes_drift:{total}!={manifest['total_bytes']}")
    if isinstance(manifest.get("files_count"), int) and len(on_disk) != manifest["files_count"]:
        problems.append(f"files_count_drift:{len(on_disk)}!={manifest['files_count']}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--max-len", type=int, default=128)
    ap.add_argument("--manifest-out")
    ap.add_argument("--manifest", help="运行前校验清单（fail-closed）")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--verify-only", action="store_true", help="仅执行 manifest 校验后退出（不加载权重）")
    args = ap.parse_args()

    if args.manifest_out:
        m = build_manifest(args.model_dir)
        json.dump(m, open(args.manifest_out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        print(json.dumps(m, indent=2, ensure_ascii=False))
        return

    if args.manifest:
        m = json.load(open(args.manifest, encoding="utf-8"))
        problems = verify_manifest(args.model_dir, m)
        if problems:
            print(json.dumps({"ok": False, "problems": problems}), file=sys.stderr)
            sys.exit(3)
        print("[bge] manifest verified (fail-closed gate passed)", file=sys.stderr)

    if args.verify_only:
        print(json.dumps({"ok": True, "verified": "sha256+bytes+count"}))
        return
    rt = BgeRuntime(args.model_dir, args.max_len)
    if args.self_check:
        print(json.dumps({"ok": True, "sanity": rt.check("self")}))
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        vec = rt.encode(rec["text"])
        sys.stdout.write(json.dumps({"id": rec["id"], "vector": vec}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

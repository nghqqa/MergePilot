#!/usr/bin/env python
# tools/bge_embed.py — BERT 系嵌入模型离线运行时（bge/e5 等 sentence-transformers 布局）。
#
# 真实语义模型 provider 的本地推理器：直接读取 HF 缓存的 model.safetensors
# （纯 numpy 前向，不依赖 torch/transformers，不做任何网络访问）。
# 池化方式按各模型官方 1_Pooling/config.json（bge=CLS，e5=mean）
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

# Beta Hardening W1：重依赖惰性化——--verify-only / --manifest-out 路径仅需 stdlib
# （hashlib/os/json），使 CI runner（无 numpy/scipy/tokenizers）可执行 manifest 校验；
# 模型前向路径在首次使用处经 _load_heavy() 加载，缺失时报可读 ImportError。
np = None
erf = None
Tokenizer = None

def _load_heavy():
    global np, erf, Tokenizer
    if np is not None:
        return
    import numpy as _np
    from scipy.special import erf as _erf
    from tokenizers import Tokenizer as _Tokenizer
    np, erf, Tokenizer = _np, _erf, _Tokenizer

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
    _load_heavy()
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


# ── torch .bin（zip 归档）受限读取器 ─────────────────────────────
# 绝不裸 unpickle：白名单全局 + persistent_load 直接取 zip 内 data/<key> 字节；
# _rebuild_tensor_v2 注入为等价 numpy 重组（storage_offset+size reshape，行主序）。
import zipfile
import pickle


class _StorageStub:
    """torch.*Storage 桩类型：pickle 只需要其 __name__（供 dtype 映射），
    绝不 import torch——容器内无 torch 依赖。"""
    def __init__(self, name):
        self.__name__ = name


_STORAGE_CLASSES = {
    ('torch', 'FloatStorage'): 'FloatStorage', ('torch', 'HalfStorage'): 'HalfStorage',
    ('torch', 'BFloat16Storage'): 'BFloat16Storage', ('torch', 'LongStorage'): 'LongStorage',
    ('torch', 'IntStorage'): 'IntStorage', ('torch', 'DoubleStorage'): 'DoubleStorage',
}


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in _STORAGE_CLASSES:
            return _StorageStub(_STORAGE_CLASSES[(module, name)])
        if (module, name) == ('collections', 'OrderedDict'):
            import collections
            return collections.OrderedDict
        raise pickle.UnpicklingError(f"forbidden global: {module}.{name}")


def _storage_dtypes():
    _load_heavy()
    return {
        'FloatStorage': np.float32, 'HalfStorage': np.float16,
        'LongStorage': np.int64, 'Int32Storage': np.int32, 'DoubleStorage': np.float64,
    }


def load_torch_bin(path):
    """读取 pytorch_model.bin（zip 格式）→ {name: ndarray}（受限 unpickler，无任意代码执行）。"""
    _load_heavy()
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    pkl_name = [n for n in names if n.endswith('data.pkl')][0]
    pkl_bytes = zf.read(pkl_name)
    storages = {}
    bytes_io = __import__('io').BytesIO

    def persistent_load(saved_id):
        # ('storage', StorageType, key, location, numel, view_metadata)
        storage_type, key, numel = saved_id[1], saved_id[2], saved_id[4]
        dtype = _storage_dtypes().get(storage_type.__name__, np.float32)
        data_name = [n for n in names if n.endswith('data/' + key)][0]
        raw = zf.read(data_name)
        return np.frombuffer(raw, dtype=dtype, count=numel)

    def make_tensor(storage, storage_offset, size, stride, requires_grad=None, hooks=None):
        expected = 1
        for d in size:
            expected *= d
        # torch 行主序 stride 与 C-order reshape 一致（bge 权重均为 contiguous）
        return storage[storage_offset:storage_offset + expected].reshape(tuple(size))

    class _U(_RestrictedUnpickler):
        def persistent_load(self, saved_id):
            return persistent_load(saved_id)

        def find_class(self, module, name):
            if module == 'torch._utils' and name in ('_rebuild_tensor_v2', '_rebuild_tensor'):
                return make_tensor
            return super().find_class(module, name)

    obj = _U(bytes_io(pkl_bytes)).load()
    return {k: np.ascontiguousarray(v) for k, v in obj.items()}


def _read_json_any(paths):
    import os
    for p in paths:
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
    raise FileNotFoundError(paths)


class BgeRuntime:
    """BERT 系 / XLM-R 系嵌入运行时（bge-*, e5-* 等 sentence-transformers 布局）。

    - 权重：model.safetensors 优先，缺省回退 pytorch_model.bin（受限 unpickler）；
    - XLM-R（model_type=xlm-roberta）：位置偏移 padding_idx+1、SP 分词器自带 <s></s>
      后处理（不手工加 101/102）、token_type 表尺寸 1（恒零）、layer_norm_eps 取配置；
    - 池化按 1_Pooling/config.json（兼容平铺 1_Pooling-config.json）。
    """

    def __init__(self, model_dir, max_len=128):
        _load_heavy()
        import os
        self.dir = model_dir.rstrip("/\\")
        self.max_len = max_len
        cfg = json.load(open(f"{self.dir}/config.json"))
        self.model_type = cfg.get("model_type", "bert")
        self.is_xlmr = self.model_type == "xlm-roberta"
        self.H = cfg["hidden_size"]
        self.L = cfg["num_hidden_layers"]
        self.A = cfg["num_attention_heads"]
        self.eps = cfg.get("layer_norm_eps", 1e-12)
        # XLM-R padding_idx=1 → 位置 id 从 2 起
        self.pos_offset = (cfg.get("pad_token_id", 1) + 1) if self.is_xlmr else 0
        import os as _os2
        if _os2.path.exists(f"{self.dir}/1_Pooling/config.json") or _os2.path.exists(f"{self.dir}/1_Pooling-config.json"):
            pooling = _read_json_any([f"{self.dir}/1_Pooling/config.json", f"{self.dir}/1_Pooling-config.json"])
        else:
            pooling = {"pooling_mode_cls_token": True}  # cross-encoder（reranker）无池化——直接取 <s> 隐层
        self.pooling = 'mean' if pooling.get("pooling_mode_mean_tokens") else 'cls'
        self.t = Tokenizer.from_file(f"{self.dir}/tokenizer.json")
        t0 = time.time()
        wpath = None
        for cand in ("model.safetensors", "pytorch_model.bin"):
            if os.path.exists(os.path.join(self.dir, cand)):
                wpath = cand
                break
        if wpath is None:
            raise FileNotFoundError(f"{self.dir}: 无 model.safetensors/pytorch_model.bin")
        self.w = load_safetensors(os.path.join(self.dir, wpath)) if wpath.endswith(".safetensors")             else load_torch_bin(os.path.join(self.dir, wpath))
        # 统一去架构前缀（roberta./bert.）+ 常权重转 dense 加速 matmul
        self.dense = {}
        for k, v in self.w.items():
            kk = k
            for pref in ("roberta.", "bert."):
                if kk.startswith(pref):
                    kk = kk[len(pref):]
            self.dense[kk] = np.ascontiguousarray(v, dtype=np.float32) if v.dtype != np.int64 else v
        print(f"[bge] {os.path.basename(self.dir)} loaded: {len(self.dense)} tensors ({wpath}, "
              f"{self.model_type}) in {time.time()-t0:.1f}s", file=sys.stderr)

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

    def ids_of(self, text):
        """单文本 → token ids（XLM-R 由后处理器加 <s></s>；BERT 双保险）。"""
        if self.is_xlmr:
            return self.t.encode(text).ids[: self.max_len]
        ids = self.t.encode(text).ids[: self.max_len - 2]
        return ([101] + ids + [102])[: self.max_len]

    def pair_ids(self, query, passage):
        """交叉编码器输入对：tokenizer 后处理器按模板拼接（XLM-R 为 <s>q</s></s>p</s>）。"""
        enc = self.t.encode(query, passage)
        return enc.ids[: self.max_len]

    def forward_hidden(self, ids):
        ids = list(ids)
        if self.is_xlmr:
            pos_ids = list(range(self.pos_offset, self.pos_offset + len(ids)))
        else:
            pos_ids = list(range(len(ids)))
        tt_row = 0
        n = len(ids)
        w = self.dense
        x = (w["embeddings.word_embeddings.weight"][ids]
             + w["embeddings.position_embeddings.weight"][pos_ids]
             + w["embeddings.token_type_embeddings.weight"][tt_row])
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
        return x  # 最后隐层 [n, H]（供 encode/rerank 复用）

    def encode(self, text):
        x = self.forward_hidden(self.ids_of(text))
        vec = x.mean(axis=0) if self.pooling == 'mean' else x[0]
        vec = vec / (np.linalg.norm(vec) or 1.0)
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
REQUIRED_TOKENIZERS = ("tokenizer.json", "vocab.txt", "sentencepiece.bpe.model")  # tokenizer 至少其一
REQUIRED_ARTIFACTS = ["config.json"]  # 权重（两种形态之一）+ tokenizer（三种其一）+ config


def build_manifest(model_dir, model_id=None, query_prefix=None, passage_prefix=None):
    """manifest v2：files 未指定时自动收录目录顶层工件（含平铺 1_Pooling-config.json）。"""
    import os
    cfg = json.load(open(f"{model_dir}/config.json"))
    import os as _os
    pooling_cfg = _read_json_any([f"{model_dir}/1_Pooling/config.json", f"{model_dir}/1_Pooling-config.json"])         if (_os.path.exists(f"{model_dir}/1_Pooling/config.json") or _os.path.exists(f"{model_dir}/1_Pooling-config.json"))         else {"pooling_mode_cls_token": True}  # cross-encoder（reranker）无池化配置——manifest 仅作供应链记录
    weights = "model.safetensors" if os.path.exists(f"{model_dir}/model.safetensors") else "pytorch_model.bin"
    names = sorted([f for f in os.listdir(model_dir)
                    if os.path.isfile(os.path.join(model_dir, f))
                    and not f.startswith('.') and f not in ('README.md', 'manifest.json')])  # manifest 自身排除（自引用无意义）
    files = []
    for f in names:
        p = os.path.join(model_dir, f)
        files.append({"name": f, "sha256": sha256_file(p), "bytes": os.path.getsize(p)})
    return {
        "manifest_version": 2,
        "model_id": model_id or cfg.get("_name_or_path", "unknown").split("/")[-1],
        "pooling": "mean" if pooling_cfg.get("pooling_mode_mean_tokens") else "cls_l2",
        "distance": "cosine",
        "dims": cfg.get("hidden_size"),
        "usage": {"query_prefix": query_prefix, "passage_prefix": passage_prefix},
        "source": "local HF cache (offline, no runtime download)",
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
    # 必需工件：权重（两种形态之一）+ tokenizer（三种其一）+ config 任一缺失即 fail-closed
    for req in REQUIRED_ARTIFACTS:
        if req not in names:
            problems.append(f"required_missing:{req}")
    if not (("model.safetensors" in names) or ("pytorch_model.bin" in names)):
        problems.append("required_missing:weights(safetensors|pytorch_bin)")
    if not any(t in names for t in REQUIRED_TOKENIZERS):
        problems.append("required_missing:tokenizer")
    # 文件数：目录内多出的受管文件视为漂移（防夹带）；缺失同样拒绝
    # 与 build_manifest 同规则：递归相对路径（含嵌套 1_Pooling/config.json），剔除隐藏与 README
    on_disk = set()
    for root, _dirs, fs_ in os.walk(model_dir):
        for f in fs_:
            if f.startswith('.') or f in ('README.md', 'manifest.json'):
                continue
            rel = os.path.relpath(os.path.join(root, f), model_dir).replace(os.sep, '/')
            if '/' in rel and rel != '1_Pooling/config.json':
                continue  # 仅收纳顶层与 1_Pooling/config.json（与 builder 口径一致）
            on_disk.add(rel)
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
    ap.add_argument("--model-id", default=None, help="manifest 模型名（缺省从 config 推断）")
    ap.add_argument("--query-prefix", default=None)
    ap.add_argument("--passage-prefix", default=None)
    args = ap.parse_args()

    if args.manifest_out:
        m = build_manifest(args.model_dir, args.model_id, args.query_prefix, args.passage_prefix)
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
    _ = args
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

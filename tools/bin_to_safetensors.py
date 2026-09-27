#!/usr/bin/env python
# tools/bin_to_safetensors.py — torch .bin → safetensors 可复演转换（G-1 缺口关闭）。
#
# 用途：把 pytorch_model.bin（受限读取器加载）确定性转换为 model.safetensors，
# 并输出输入/输出 SHA256 对应表——任何人都可重跑本脚本独立复演转换链。
# 确定性：同一输入字节 → 同一输出字节（safetensors 按键序写入，值按加载序）。
# 输出：{input_bin_sha256, output_sft_sha256, tensors, bf16_converted, timestamp}
# 用法：python tools/bin_to_safetensors.py --model-dir DIR [--out-suffix .repro]
#        （--out-suffix 写旁路文件，不覆盖现有 model.safetensors，供哈希比对）
import argparse
import hashlib
import importlib.util
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('bge_embed', os.path.join(HERE, 'bge_embed.py'))
bge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bge)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model-dir', required=True)
    ap.add_argument('--out-suffix', default='.repro', help='输出旁路文件后缀（默认 .repro，不覆盖原文件）')
    args = ap.parse_args()

    from safetensors.numpy import save_file
    d = args.model_dir
    bin_path = os.path.join(d, 'pytorch_model.bin')
    if not os.path.exists(bin_path):
        print(json.dumps({'fatal': f'pytorch_model.bin not found: {bin_path}'}), file=sys.stderr)
        sys.exit(3)

    input_sha = sha256_file(bin_path)
    tensors = bge.load_torch_bin(bin_path)
    out = {}
    bf16 = 0
    for k, v in tensors.items():
        if v.dtype == np.uint16:  # BFloat16Storage 桩 → fp32
            u = v.astype(np.uint32) << 16
            out[k] = u.view(np.float32)
            bf16 += 1
        else:
            out[k] = v.astype(np.float32) if v.dtype != np.int64 else v

    out_path = os.path.join(d, f'model{args.out_suffix}.safetensors')
    save_file(out, out_path)
    output_sha = sha256_file(out_path)

    record = {
        'model_dir': os.path.basename(d.rstrip('/\\')),
        'input_bin_sha256': input_sha,
        'output_safetensors_sha256': output_sha,
        'output_file': os.path.basename(out_path),
        'tensors': len(out),
        'bf16_converted': bf16,
        'deterministic': '同一输入字节→同一输出字节（按键序+加载序写入）',
        'timestamp': __import__('datetime').datetime.now().isoformat(),
    }
    print(json.dumps(record, indent=2, ensure_ascii=False))
    # 旁路文件比对后由调用方删除；本脚本不修改任何现有文件


if __name__ == '__main__':
    main()

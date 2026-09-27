#!/usr/bin/env python
# tools/rerank_score.py — bge-reranker-v2-m3 交叉编码器打分（RAG_ZH_MODEL_RERANKER_EVALUATION）。
#
# 输入：stdin 每行 {"qid":..., "query":..., "passage":...}
# 输出：stdout 每行 {"qid":..., "score": float}
# 启动即按 manifest v2 fail-closed 校验（--manifest）；绝不带病打分。
# 计分：RoBERTa 分类头 dense→tanh→out_proj 的原始 logit（官方用法，不 softmax）。
import argparse
import json
import sys
import time

import numpy as np
from scipy.special import erf  # noqa: F401（经 bge_embed 间接使用）

import importlib.util
import os
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('bge_embed', os.path.join(HERE, 'bge_embed.py'))
bge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bge)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model-dir', required=True)
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--max-len', type=int, default=192)
    args = ap.parse_args()

    problems = bge.verify_manifest(args.model_dir, json.load(open(args.manifest, encoding='utf-8')))
    if problems:
        print(json.dumps({'fatal': 'manifest verify failed', 'problems': problems}), file=sys.stderr)
        sys.exit(3)
    print('[rerank] manifest verified (fail-closed gate passed)', file=sys.stderr)

    rt = bge.BgeRuntime(args.model_dir, max_len=args.max_len)
    w = rt.dense
    # 分类头（RobertClassificationHead：dense→tanh→out_proj；容忍 roberta. 前缀已剥离）
    cls_dense_w = w['classifier.dense.weight']
    cls_dense_b = w['classifier.dense.bias']
    cls_out_w = w['classifier.out_proj.weight']
    cls_out_b = w['classifier.out_proj.bias']

    n_done = 0
    t0 = time.time()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        ids = rt.pair_ids(r['query'], r['passage'])
        x = rt.forward_hidden(ids)
        h = x[0]  # <s>/[CLS]
        h = np.tanh(h @ cls_dense_w.T + cls_dense_b)
        score = float((h @ cls_out_w.T + cls_out_b)[0])
        sys.stdout.write(json.dumps({'qid': r['qid'], 'score': round(score, 6)}, ensure_ascii=False) + '\n')
        n_done += 1
        if n_done % 50 == 0:
            sys.stderr.write(f'[rerank] {n_done} pairs, {n_done/(time.time()-t0):.2f}/s\n')
            sys.stdout.flush()
    dt = time.time() - t0
    print(f'[rerank] done: {n_done} pairs in {dt:.0f}s ({n_done/max(dt,0.001):.2f}/s)', file=sys.stderr)


if __name__ == '__main__':
    main()

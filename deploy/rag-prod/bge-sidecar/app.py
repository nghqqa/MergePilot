#!/usr/bin/env python
# deploy/rag-prod/bge-sidecar/app.py — bge-large-en-v1.5 嵌入 sidecar（PRODUCTION_RAG_READINESS）。
#
# 形状兼容 openai /v1/embeddings 的子集：POST /embed {input:[...], model} →
# {data:[{embedding:[...]}...]}；GET /manifest 返回 manifest 文件原始字节
# （sha256 与 console 侧 RAGTRIAL_EMBED_EXPECTED_MANIFEST 精确比对）；
# GET /health。
#
# fail-closed：启动时用 --manifest 逐文件校验模型工件 sha256；任何缺失/漂移
# 直接退出非零——绝不带病服务。运行期不做任何模型下载。

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 同目录引入运行时（容器内 /app；本地测试时按相对路径）
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bge_embed  # noqa: E402

MODEL_DIR = os.environ.get("BGE_MODEL_DIR", "/app/model")
MANIFEST = os.environ.get("BGE_MANIFEST", "/app/bge-large-en-v1.5.manifest.json")
PORT = int(os.environ.get("BGE_PORT", "8080"))

# 启动即校验（fail-closed）
problems = bge_embed.verify_manifest(MODEL_DIR, json.load(open(MANIFEST, encoding="utf-8")))
if problems:
    print(json.dumps({"fatal": "model manifest verify failed", "problems": problems}),
          file=sys.stderr)
    sys.exit(3)

RT = bge_embed.BgeRuntime(MODEL_DIR, max_len=int(os.environ.get("BGE_MAX_LEN", "128")))
MANIFEST_BYTES = open(MANIFEST, "rb").read()
USAGE = json.loads(MANIFEST_BYTES).get("usage") or {}
MODEL_NAME = json.loads(MANIFEST_BYTES).get("model_id", "unknown")


def _apply_prefix(text, mode):
    # e5 系官方用法：query/passage 前缀（manifest.usage 声明；未声明=原样）
    if mode == "query" and USAGE.get("query_prefix"):
        return USAGE["query_prefix"] + text
    if mode == "passage" and USAGE.get("passage_prefix"):
        return USAGE["passage_prefix"] + text
    return text


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "model": MODEL_NAME, "dims": json.loads(MANIFEST_BYTES).get("dims")})
        elif self.path == "/manifest":
            # 原始字节回放——sha256 与 pin 精确一致
            self._send(200, MANIFEST_BYTES)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/embed":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, {"error": "invalid json"})
            return
        inputs = body.get("input")
        if not isinstance(inputs, list) or not inputs or not all(isinstance(t, str) for t in inputs):
            self._send(400, {"error": "input:[string] required"})
            return
        if len(inputs) > 16:
            self._send(429, {"error": "batch too large (max 16)"})
            return
        mode = body.get("mode")  # 'query' | 'passage' | None（模型用法前缀）
        data = [{"embedding": RT.encode(_apply_prefix(t, mode))} for t in inputs]
        self._send(200, {"data": data, "model": body.get("model", MODEL_NAME)})

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("[bge-sidecar] " + fmt % args + "\n")


if __name__ == "__main__":
    print(f"[bge-sidecar] listening :{PORT}, model={MODEL_DIR} (manifest verified)", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

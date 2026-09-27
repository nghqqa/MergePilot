# bge-sidecar 镜像（PRODUCTION_RAG_READINESS）。
# 构建期安装固定版本依赖（numpy/scipy/tokenizers——非模型，pypi 锁版本）；
# 模型权重不在镜像内：以只读 bind mount 挂载主机 HF 缓存快照，
# 启动时按 manifest sha256 fail-closed 校验，任何漂移拒绝服务。
# 绝无运行时模型下载。
# 基础镜像 python:3.11-slim 为本地已有镜像（pull_policy: never 离线构建；
# digest pin 待发布发行物时补齐——见报告 §外部依赖）。
FROM python:3.11-slim

RUN pip install --no-cache-dir \
      numpy==1.26.4 \
      scipy==1.13.1 \
      tokenizers==0.22.2

WORKDIR /app
COPY deploy/rag-prod/bge-sidecar/app.py ./app.py
COPY tools/bge_embed.py ./bge_embed.py
COPY deploy/rag-prod/bge-large-en-v1.5.manifest.json ./bge-large-en-v1.5.manifest.json

ENV BGE_MODEL_DIR=/app/model BGE_MANIFEST=/app/bge-large-en-v1.5.manifest.json BGE_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)" || exit 1
CMD ["python", "app.py"]

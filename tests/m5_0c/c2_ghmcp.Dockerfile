# C2 github-mcp bridge image (minimal SSE->stdio, mcp==1.28.1 — avoids mcp-proxy
# version drift) + restricted c2_delete_test_branch cleanup tool.
#
# Base images PINNED to immutable RepoDigests verified locally in
# MergePilot-Test (no :latest, no implicit pull on build):
#   ghcr.io/github/github-mcp-server  v1.8.0
#     @sha256:d5a18c04b92714c309eb46a2305087e91a4dbd80420f6e462656699f95093520
#   python 3.12-slim
#     @sha256:9e869b0816f5537709825b49e62dc86d1c2691eff19b05c1d4dc3a07992cc052
# Bridge deps pinned: mcp 1.28.1, starlette 0.41.0, uvicorn 0.32.0,
#   httpx 0.28.1, anyio 4.14.2.
FROM ghcr.io/github/github-mcp-server@sha256:d5a18c04b92714c309eb46a2305087e91a4dbd80420f6e462656699f95093520 AS github

FROM python@sha256:9e869b0816f5537709825b49e62dc86d1c2691eff19b05c1d4dc3a07992cc052
RUN pip install --no-cache-dir \
      "mcp==1.28.1" \
      "starlette==0.41.0" \
      "uvicorn==0.32.0" \
      "httpx==0.28.1" \
      "anyio==4.14.2"
COPY --from=github /server/github-mcp-server /usr/local/bin/github-mcp-server
WORKDIR /app
ENTRYPOINT ["python", "/app/bridge.py"]

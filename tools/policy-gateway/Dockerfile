# Policy Gateway — 角色 token 认证的 MCP 前置网关(M3-B)。
# 前端给 mcporter(SSE),后端连 github-mcp bridge(SSE)。
# 不持 GitHub PAT;PAT 只在 github-mcp 容器(私有 mcp-backend-net)。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY gateway.py /app/gateway.py
COPY policy.yaml /app/policy.yaml

ENV LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8083
EXPOSE 8083

# 健康检查:无 token 访问 /health 应 404(未实现 health 路由)或 401,容器活着即可
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import socket;s=socket.socket();s.settimeout(2);s.connect(('127.0.0.1',8083));print('ok')" || exit 1

ENTRYPOINT ["python", "/app/gateway.py"]

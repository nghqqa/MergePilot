# 凭证隔离桥:把 github 官方 MCP server(stdio)桥接成网络 SSE 服务。
# PAT 只在本容器 env(mcp-proxy --pass-environment 转发给 stdio 子进程),
# worker 经 mcporter 连 SSE,不持有任何 GitHub 凭证。
FROM ghcr.io/github/github-mcp-server:latest AS github

FROM python:3.12-slim
RUN pip install --no-cache-dir "mcp-proxy>=0.3.1"
COPY --from=github /server/github-mcp-server /usr/local/bin/github-mcp-server

ENV MCP_PROXY_PORT=8082
EXPOSE 8082
# mcp-proxy: SSE/HTTP 服务监听 8082,把请求桥接给 stdio 子进程;--pass-environment 转发 env(含 PAT)
ENTRYPOINT ["mcp-proxy", "--host", "0.0.0.0", "--port", "8082", "--pass-environment", "--", "/usr/local/bin/github-mcp-server", "stdio"]

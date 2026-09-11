# 安装与配置

## 前提条件

- AgentTeams（原名 Hiclaw）运行时已部署
- CoPaw Worker 容器已启动（4 worker）
- MCP 客户端已在 agent.json `mcp.clients` 中启用

## 安装方式

### 方式一：MCP stdio 注册（推荐）

在 CoPaw Worker 的 `agent.json` 中添加：

```json
{
  "mcp": {
    "clients": {
      "rag_synthetic": {
        "name": "rag_mcp",
        "enabled": true,
        "transport": "stdio",
        "command": "node",
        "args": ["/path/to/rag-mcp-server.mjs"],
        "env": {
          "RAG_ENDPOINT": "http://<demo-platform-host>:<port>/api/rag/search",
          "RAG_AUDIT_ENDPOINT": "http://<demo-platform-host>:<port>/api/rag/toolspan-audit"
        }
      }
    }
  }
}
```

重启 Worker 后 MCP 客户端自动连接，工具注册进 Agent 函数列表。

### 方式二：直接 HTTP 调用（不需要 MCP）

Skill 后端是标准 HTTP JSON API，可直接通过 shell curl 或 HTTP 客户端调用：

```bash
# RAG 检索
curl http://127.0.0.1:4173/api/rag/search?q=高危变更

# 数据库 Branch 创建
curl -X POST http://127.0.0.1:4173/api/db/branch/create \
  -H "content-type: application/json" \
  -d '{"candidate_id": "candidate-c"}'
```

## 版本

当前版本 1.0.0（2026-08-31）。接口契约见 [SKILL.md](SKILL.md)。

## 分发

随 MergePilot 演示平台一起分发（MergePilot-demo.zip 内含全部依赖）。

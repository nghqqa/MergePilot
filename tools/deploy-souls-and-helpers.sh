#!/bin/bash
# 部署 MergePilot Worker 合同（SOUL / Manager 状态机 / python GitHub MCP
# helpers）到 HiClab（MinIO 持久化 + worker 容器即时生效）。
# M8-A2-d 起为正式仓库资产部署的薄包装：源文件全部来自本仓库
# config/souls 与 tools/agentteams，不再依赖任何仓库外目录。
# 用法:
#   ./deploy-souls-and-helpers.sh            # dry-run（无副作用）
#   ./deploy-souls-and-helpers.sh --apply    # 实际部署
# MinIO 凭据仅经环境变量 HICLAW_MINIO_USER / HICLAW_MINIO_PASS 传入，
# 绝不进入 argv 或日志。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/agentteams/deploy_worker_contracts.py" "$@"

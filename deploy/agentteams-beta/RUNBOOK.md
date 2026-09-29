# AgentTeams Beta 运维手册（Wave 3.4）

适用：Developer Edition Beta 受控运行（非 Enterprise Production）。栈=`deploy/agentteams-beta/`。

## 1. 首次部署
```bash
# 凭据生成（一次性；绝不写入 Git/Compose/镜像）
export ATB_ADMIN_PASSWORD=$(openssl rand -hex 12)  ATB_MINIO_PASSWORD=$(openssl rand -hex 12)
export ATB_MANAGER_PASSWORD=$(openssl rand -hex 12) ATB_REGISTRATION_TOKEN=$(openssl rand -hex 16)
export ATB_LLM_KEY=<专用一次性非生产 deepseek key>
export ATB_API_PORT=28462
cd deploy/agentteams-beta && docker compose -p agentteams-beta up -d
bash provision-workers.sh          # 四具名 Agent（幂等，可重跑自愈）
```
MergePilot 侧（`MU_EXECUTOR=agentteams` + 双传输配置）：
```bash
MU_EXECUTOR=agentteams
MU_AGENTTEAMS_BASE_URL=http://<controller-host>:$ATB_API_PORT
MU_AGENTTEAMS_TOKEN=<controller cli-token>
MU_AGENTTEAMS_RUNTIME=copaw  MU_AGENTTEAMS_MODEL=deepseek-chat
MU_AGENTTEAMS_MATRIX_URL=http://<controller-host>:6167   # 见 §9 网络说明
MU_AGENTTEAMS_MATRIX_USER=agentteams-beta-admin
MU_AGENTTEAMS_MATRIX_PASSWORD=$ATB_ADMIN_PASSWORD
```
token 获取：`docker exec agentteams-beta-ctrl sh -c 'tr -d "\n\r" < /var/run/agentteams/cli-token'`（进程环境注入 MergePilot，不落盘）。

## 2. 健康检查
- 栈：`docker compose -p agentteams-beta ps`（controller healthy）
- 四 Agent：`curl -H "Authorization: Bearer $TOK" .../api/v1/workers` → mergepilot-* 全 Running
- MergePilot 侧：`GET /api/mu/agentteams-status`（PlatformAdmin）→ `runtime_state: active`、`workers_ready: 4`
- worker 心跳：`docker exec agentteams-beta-worker-mergepilot-<role> sh -c 'ls -la --time-style=full-iso /root/.copaw-worker/mergepilot-<role>/.copaw/matrix_sync_token'`（mtime 应 <60s）

## 3. 故障场景
| 场景 | 症状 | 处置 |
|---|---|---|
| worker 不完整 | status `workers_ready<4` / gate `AT_WORKERS_INCOMPLETE` | 重跑 `provision-workers.sh`（幂等自愈） |
| gateway 失败 | worker LLM 422/404（gateway init 断链为上游已知） | 确认 openclaw.json `primary=deepseek-direct`（脚本 §3 维护）；重跑脚本 |
| Matrix 故障 | `MT_LOGIN_*`/`MT_READ_*`/`MT_SEND_*`，run 停 FIX_QUEUED | `docker exec agentteams-beta-ctrl supervisorctl status tuwunel`；重启 tuwunel；MergePilot 侧自动重登（token 过期路径有测） |
| MinIO 故障 | worker 反复 "Worker config not ready" | controller 日志 `/var/log/agentteams/minio*.log`；`docker restart agentteams-beta-ctrl` 后**必须 restart 四 worker**（共享 netns 断链）并重跑 provision |
| LLM 故障 | `MT_REPLY_TIMEOUT` 死信 / preflight 401 | 核 key 有效性（`agt llm-preflight`）；换新一次性 key → 重跑 provision §3 |
| controller 重启后 | worker 全部失联 | `for r in leader reviewer fixer verifier; do docker restart agentteams-beta-worker-mergepilot-$r; done && bash provision-workers.sh` |

## 4. 重启恢复
- controller：`docker restart agentteams-beta-ctrl`（数据卷保留：K8s/Matrix/MinIO 状态）→ restart 四 worker → provision（reconcile 恢复 phase）
- 单 worker：`docker restart agentteams-beta-worker-mergepilot-<role>`（sync 心跳自动恢复，已验证）

## 5. 凭据轮换
1. 生成新值 → `docker compose -p agentteams-beta up -d`（重建 controller，卷保留）→ provision → MergePilot env 更新重启
2. LLM key：重跑 provision §3（仅 openclaw.json 刷新）+ MergePilot 无需动（key 不经 MergePilot）

## 6. 备份/恢复
- 范围：`agentteams-beta-data` 卷（MinIO：worker 凭据/workspace/项目元数据）+ controller cli-token
- `docker run --rm -v agentteams-beta-data:/data -v $PWD:/backup alpine tar czf /backup/atb-data.tgz /data`
- 恢复：down → 解包到卷 → up → provision。**worker Matrix 凭据在卷内——恢复后无需重新注册**

## 7. 升级
1. 新 digest 记录+Trivy（RUNBOOK §供应链）→ 改 compose tag → `docker compose up -d` → provision
2. 升级前备份 §6；上游 changelog 核对 gateway/re-bridge/DAG 缺陷状态

## 8. 回滚
- compose tag 回退到本清单锁定 digest → up -d → provision → `agentteams-status` active
- MergePilot 侧无需回滚（adapter 契约未变时）

## 9. 卸载（完整）
```bash
docker compose -p agentteams-beta down -v --remove-orphans
docker rm -f agentteams-beta-worker-mergepilot-{leader,reviewer,fixer,verifier} 2>/dev/null
docker volume rm -f agentteams-beta-worker-mergepilot-{leader,reviewer,fixer,verifier}-auth 2>/dev/null
docker network rm agentteams-beta_atnet 2>/dev/null
unset ATB_ADMIN_PASSWORD ATB_MINIO_PASSWORD ATB_MANAGER_PASSWORD ATB_REGISTRATION_TOKEN ATB_LLM_KEY
```

## 10. 网络说明（Matrix URL）
embedded 栈的 Matrix(6167)/MinIO(9000) 仅 controller netns 内监听。MergePilot 与 controller 不同主机/网络时需加发布（`127.0.0.1:6167:6167`，仅回环/内网）或经受控反代；同 Docker 主机可将 MergePilot backend 加入 `agentteams-beta_atnet` 直用 `http://agentteams-beta-controller:6167`。**禁止公网暴露 6167/9000/8088**。

## 11. 供应链记录（2026-09-29 裁决）
| 镜像 | digest(sha256) | 来源 | 许可 | Trivy H/C |
|---|---|---|---|---|
| agentteams-embedded:223ddc2 | ae47995d209f… | github.com/agentscope-ai/AgentTeams@223ddc2b 本地构建（2026-08-27） | Apache-2.0 | 538/39 |
| copaw-worker:223ddc2-agentloop-v5fix | 4be8eccfe4fb… | 同上（实战迭代镜像） | Apache-2.0 | 237/24 |
| manager:223ddc2 | cafca0c1dc16… | 同上 | Apache-2.0 | 847/46 |
| worker-agent:223ddc2 | 961b04c5f2b0… | 同上 | Apache-2.0 | 847/46 |

- 无官方签名镜像 → **不宣称供应链完全验证**；证据=digest 锁定+本地扫描（Trivy 缓存 DB 2026-09-28 版、Java DB 跳过——限制如实登记）
- 风险接受（Beta）：R-1 漏洞多为 OS 包且暴露面仅内网+LLM 出站；R-2 controller 挂 docker proxy socket（上游设计，容器命名空间受控）；R-3 controller 不可 read-only/非 root（supervisord 全家桶写需求）
- 阻断项核查：可远程利用且命中当前暴露面=无；digest 未固定=无；来源不可验证=无；镜像含真实凭据=无；worker privileged/宿主 socket=无（仅 controller，R-2 接受）

## 12. 安全红线（重申）
- 四 Agent 不持有 GitHub/DB/OAuth/MergePilot-LLM 凭据；载荷仅 untrusted_findings 白名单 ≤2KiB
- Fixer 仅 dry-run 文本；Verifier 独立判定；MergePilot Leader 终裁
- 无 approve/merge/push/仓库写入能力；branch protection 不因本栈改变

# 生产栈手工容器重启策略（2026-10-21 加固）

## 背景

宿主机重启实证（2026-10-21）：以下手工 `docker run` 容器因缺少重启策略，引擎重启后
未能自动恢复，需运维逐个手工 start（agentteams-beta-ctrl、trial-bge）。
本文件为这些容器的**权威重启策略记录**：重建时务必带 `--restart unless-stopped`；
已运行容器用 `docker update --restart unless-stopped <name>` 就地补齐（不停机）。

## 策略清单（2026-10-21 已就地生效）

| 容器 | 策略 | 说明 |
|---|---|---|
| agentteams-beta-ctrl | unless-stopped | 2026-10-21 docker update 补齐（原 no） |
| trial-bge | unless-stopped | 2026-10-21 docker update 补齐（原 no；18700 RAG sidecar） |
| beta-mp-pg | unless-stopped | 已有策略（核实确认，未变更） |
| at-auth-proxy | unless-stopped | 已有策略；启动顺序依赖 ctrl（nginx 启动时解析 upstream，ctrl 未起则 emerg 退出重启循环直至 ctrl 可解析——unless-stopped 重试循环即自愈） |
| beta-mp-console / mp-trial-console | unless-stopped | 已有策略 |

## 回滚

`docker update --restart no agentteams-beta-ctrl trial-bge`（恢复 no 策略；
不影响运行中进程）。

## 网络别名（2026-10-21 事故固化：重建必带）

agentteams-beta-ctrl 在 agentteams-beta_atnet 上必须携带**两个**网络别名
（与 deploy/agentteams-beta/docker-compose.yml 的权威声明一致）：

| 别名 | 消费方 |
|---|---|
| `agentteams-beta-controller` | 容器内三服务统一寻址：AGENTTEAMS_CONTROLLER_URL(:8090)、AGENTTEAMS_FS_ENDPOINT(:9000，内嵌 MinIO)、AGENTTEAMS_MATRIX_URL(:6167) |
| `agentteams-beta-matrix` | AGENTTEAMS_MATRIX_DOMAIN——worker 侧 Matrix 消息寻址域（@…:agentteams-beta-matrix:6167） |

**事故实证（2026-10-21）**：今晨主机重启恢复中 ctrl 以手工 `docker run` 重建
（Created=Started 同秒），网络接入与 IP 正确但别名漏带 → 容器内解析
`agentteams-beta-controller` 失败 → projects 接口因 DNS 返回 500（MinIO 本体健康）。
代理链（console→at-auth-proxy→nginx→`agentteams-beta-ctrl:8090` 容器名）不受影响，
仅别名消费方受损。

重建命令必带（IP 按当时实值，当前=192.168.96.6）：

    docker run -d --name agentteams-beta-ctrl --restart unless-stopped \
      --network agentteams-beta_atnet --ip 192.168.96.6 \
      --network-alias agentteams-beta-controller --network-alias agentteams-beta-matrix \
      …（其余参数以 inspect 保全件为准，不凭记忆重造）

重启/重建后 60 秒核查（巡检项）：

    docker inspect agentteams-beta-ctrl --format '{{(index .NetworkSettings.Networks "agentteams-beta_atnet").Aliases}}'
    docker exec agentteams-beta-ctrl getent hosts agentteams-beta-controller agentteams-beta-matrix

别名丢失的最小修复（不重启容器、不断其他网络；已连接端点不可重复 network connect，
须 disconnect 后带原 IP+全量别名重连；失败立即不带别名重连还原）：

    docker network disconnect agentteams-beta_atnet agentteams-beta-ctrl
    docker network connect --ip 192.168.96.6 \
      --alias agentteams-beta-controller --alias agentteams-beta-matrix \
      agentteams-beta_atnet agentteams-beta-ctrl

注意：nginx upstream 按容器名解析且启动时缓存——修复保持原 IP 时 proxy 无需 reload；
若 IP 变更须 `docker exec at-auth-proxy nginx -s reload`。

## 边界

- unless-stopped 不等同于进程级守护：容器内进程崩溃仍由引擎按重启策略拉起，
  但**手工 docker rm/stop 后不会自动重建**（配置漂移需人工介入）。
- 启动顺序：ctrl 先于 at-auth-proxy；beta-mp-pg 先于 beta-mp-console；
  trial-bge 先于 mp-trial-console（RAGTRIAL_EMBED_ENDPOINT 依赖）。
  except ctrl/proxy 对外，其余顺序依赖由重试策略兜底。

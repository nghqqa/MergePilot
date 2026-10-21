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

## 边界

- unless-stopped 不等同于进程级守护：容器内进程崩溃仍由引擎按重启策略拉起，
  但**手工 docker rm/stop 后不会自动重建**（配置漂移需人工介入）。
- 启动顺序：ctrl 先于 at-auth-proxy；beta-mp-pg 先于 beta-mp-console；
  trial-bge 先于 mp-trial-console（RAGTRIAL_EMBED_ENDPOINT 依赖）。
  except ctrl/proxy 对外，其余顺序依赖由重试策略兜底。

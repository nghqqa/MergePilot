# AgentTeams 官方入口端口对照（回应"是否基于官方底层"）

## 官方架构 ↔ 本演示运行时（组件级对应，全部真实启用）
| 官方组件 | 官方入口 | 本运行时 | 状态 |
|---|---|---|---|
| Tuwunel (Matrix) 自建 IM | K8s 内部 svc | p14h2-wd-ctrl:6167（127.0.0.1 已发布） | ✅ 一直启用（全部 agent 通信 + 事件历史） |
| MinIO 集中存储 | K8s 内部 svc | p14h2-wd-ctrl:9000（127.0.0.1） | ✅ 一直启用 |
| Higress AI 网关 | K8s: 18080 (port-forward) | p14h2-wd-ctrl:8080（网内） | ✅ 启用（LLM 代理 + 凭证，多 consumer key-auth） |
| Element Web 登录页 | http://127.0.0.1:18088/#/login | **p14h2-wd-element-web（本次新增）** | ✅ 2026-08-29 11:4x 起提供，指向真实 homeserver 6167 |
| Controller/CRD reconcile | K8s 控制面 | agentteams-embedded:223ddc2（Docker reconcile 模式） | ✅ worker 容器即由 controller 创建/管理 |

## 官方 Element Web 部署证据
- 镜像: vectorim/element-web:latest（官方 Element Web 发行镜像）
- 容器: p14h2-wd-element-web，网络 p14h2-wd-net，发布 127.0.0.1:18088->80
- config.json: default_hs_url=http://127.0.0.1:6167（即本运行时真实 Tuwunel）、brand=AgentTeams
- 验证: GET http://127.0.0.1:18088/ → 200；GET /config.json 返回上述配置；
  GET http://127.0.0.1:6167/_matrix/client/versions → v1.15（homeserver 存活）

## 底层同一性的既有证据链
- 镜像/源码: agentteams-embedded:223ddc2、agentteams/worker-agent:223ddc2、
  agentteams/copaw-worker:223ddc2-build1/2（由官方 Dockerfile 于源码 commit 223ddc2 构建）
- Worker 容器(agentteams-worker-*)由 controller 的 reconcile 循环创建/管理（非手工编排）
- Matrix 房间事件历史、MinIO bucket、Higress consumer、unified_queue/tool_guard 等框架
  行为日志齐备（见各 PHASE14 证据目录）

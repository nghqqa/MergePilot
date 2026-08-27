# Phase 7.3E · 外部资源恢复（BLOCKED_CONFIGURATION 存证）

## 裁决
MERGEPILOT_AGENTLOOP_PHASE7_REAL_AGENTTEAMS_BLOCKED_CONFIGURATION

## 因果链（阶段→调用→返回码→typed failure）
1. start --m4f（默认模式）→ controller FATAL 缺 COORDINATOR_TOKEN（该模式无编排令牌属设计）→ 判定四 Agent 载体=--github-e2e
2. start --m4f --github-e2e → EXIT_PRECHECK / GITHUB_E2E_PREREQUISITES_INCOMPLETE（github-e2e.json 缺失）→ 复用历史 20 键 provision
3. 资源核验 → tuwunel 容器/镜像/数据三重 ABSENT；仓库内无 provisioner → 外部资源不可在本机现状恢复

## 恢复路径（重跑 READY 的唯一前置）
按 M8-GH-4 外部资源工作负载：获取 tuwunel 镜像（来源需向维护者确认）→ 注册 5 个 m8gh4-* 用户 → 重建房间 → 重发 5 个 access token → 重写 4×mcporter/controller token/room-map → 刷新 state/github-e2e.json 的 tuwunel_ip → start --run-id gh-<new> --m4f --github-e2e

## 分类账
VERIFIED: 链路与门禁行为（typed fail-closed）；20 键配置复用；五项资源存在性核验
NOT_VERIFIED: 真实四 Agent 链路、AgentLoop AI Agent 语义页
BLOCKED_CONFIGURATION: 外部 Matrix 资源缺失（本文件）
未发生: 模型调用 / 伪造 span

## L4（7.3G 恢复执行）
tuwunel 已恢复运行（本地 digest 验证镜像、restart 策略、172.22.0.2:6167、卷持久）；5 用户注册+token whoami 全过；房间可创建可列出；**阻断点=/rooms/{id}/* 路由 200↔M_UNRECOGNIZED 抖动**，邀请/加入不可用 → BLOCKED_RUNTIME（Matrix 资源未完全可用）。需维护者确认内嵌 tuwunel 行为（怀疑 DELETE_ROOMS_AFTER_LEAVE 房间折叠 / server_name 路由 / 不稳定房间版本）。
## L5（7.3H 稳定性观测 + BLOCKED_UPSTREAM）
保活下路由 28/28 稳定（invite 200/join 200/403 已在房语义正确）→ 排除 tuwunel 本体缺陷；VM 回收期后同实例复现 createRoom-200 但 /rooms/* M_UNRECOGNIZED（响应体已存）。契约发现：四 Agent mxid=裸 localpart（已按契约注册 200）。单变量实验（DELETE_ROOMS 双开关=false 新容器对照）为第一恢复步骤，未跑完即收口 → 裁决 BLOCKED_UPSTREAM。
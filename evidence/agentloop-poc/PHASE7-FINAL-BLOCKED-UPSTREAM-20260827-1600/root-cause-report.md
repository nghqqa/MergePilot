# Phase 7 最终根因报告 — BLOCKED_UPSTREAM_TUWUNEL_ROUTE

## 决定性复现请求矩阵（单一稳定 boot、唯一 172.25.0.2 占用容器）
| # | 端点 | 结果 |
|---|---|---|
| 1 | GET / | 200（hewwo greeting）|
| 2 | POST /_matrix/client/v3/createRoom | 200（room_id 正常返回）|
| 3 | GET /rooms/{id}/state（刚创建）| **404 M_UNRECOGNIZED** ×6/6 |
| 4 | POST /rooms/{id}/invite（刚创建）| **404 M_UNRECOGNIZED** ×6/6 |
| 5 | GET /_matrix/client/v3/joined_rooms | 200（列出全部历史房间，含 404 的）|

对照组（全部排除）：Windows/WSL-host/Matrix-network 三视角一致；WSL 生命周期保活会话内 uptime 恒定；认证/room_id 编码/r0-v3/成员集合/DELETE-FORGET 开关/transport 模块/Docker capability 逐一排除。

## 生命周期对照
| 场景 | 房间路由 |
|---|---|
| 全新卷 + 首个容器生命周期内建房 | ✅ 全 200（16/16 探针、5/5 成员）|
| 容器 docker restart（同卷）| ✅ 旧房/新房全 200 |
| daemon restart（同卷）| ✅ 全 200 |
| **容器 rm+run 重建（同卷）后新建房** | ❌ **404 M_UNRECOGNIZED（确定复现，跨 p7g/p7i 双卷）**|

## 结论
内嵌 tuwunel 1.5.0-48（hiclaw-embedded:v1.1.2，digest sha256:5f8b42fd…）在容器重建后，新创建房间不进入路由注册——上游缺陷。非本项目可修。

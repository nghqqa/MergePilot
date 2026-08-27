# Phase 7.3H3 冷启动判别（BLOCKED_RUNTIME_DIAGNOSIS_PENDING）

## 冷启动执行记录
- wsl --terminate → Stopped → service docker start → tuwunel 自启（restarts=0）
- RocksDB v17 序列 3068 完整载入，Listening 正常，greeting 正常
- 冷启动后判别（同容器 a7f75549，10 轮×3 端点）：joined_rooms 200 ×10；
  state/members **400 ×10（确定性，带 Authorization）**；formal 房 createRoom 200
- 邀请四 Agent 未入房（members 仅 controller）——与 400 同因待查

## 与既往时代对照（同一镜像/同一脚本形态）
| 时代 | state/members | invite |
|---|---|---|
| fnka 期（VM 保活） | 200 | 200→403 已在房 |
| 7IK 期 | 200（joined_members 含 2 成员） | 404 M_UNRECOGNIZED |
| 本次冷启动 | **400 确定性** | 邀请未入房 |

## 判定
证据不足（400 响应体未及取出）→ BLOCKED_RUNTIME_DIAGNOSIS_PENDING

## 下一步（单点，15 分钟内可完成）
p7h3-probe400.py 已带 token 版三连（old-state/old-members/invite-old）——运行后 400 body 即命名根因
（预判方向：conduwuit 对**带端口 server_name** 的房间在冷启动后要求 `?` 参数或路由版本差异）。
随后按判定表：配置可修 → CONFIGURATION_READY_FOR_RESTORE；确证上游 → BLOCKED_UPSTREAM_TUWUNEL_ROUTE。

## 最终判别（7.3H3 收口）→ BLOCKED_UPSTREAM_TUWUNEL_ROUTE
同 boot 内全新房间 createRoom=200、state/invite=404 M_UNRECOGNIZED（6/6 确定性）；四变量排除完毕（保活/开关/实例切换/generic 损坏），剩余唯一变量=多时代 RocksDB 卷 × 内嵌构建路由器。实用恢复=丢弃该卷+重跑两份幂等供给脚本（分钟级）。裁决本 Phase 7 以 BLOCKED_UPSTREAM_TUWUNEL_ROUTE 收卷，全部修复与四层根因链已入证据。

## 最终判别（决定性复现）→ BLOCKED_UPSTREAM_TUWUNEL_ROUTE
全新卷首会话建房路由全通（16/16+5/5）；容器 rm+run 重建（同卷）后新建房间 createRoom=200 但房间路由 404 M_UNRECOGNIZED（跨 p7g/p7i 双卷、多实例确定复现）。属内嵌 tuwunel 上游缺陷，非本项目可修。恢复路径=升级/更换内嵌 Matrix 构建（维护者决策），或以「首会话建全资源 + 此后仅 docker restart（已验证可行）」的受控生命周期规避。
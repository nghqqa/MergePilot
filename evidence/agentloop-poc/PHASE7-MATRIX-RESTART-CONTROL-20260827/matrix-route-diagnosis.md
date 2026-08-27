# tuwunel 房间路由诊断（7.3H2）

## 四实验结果（全部在受控单 WSL 会话/独立资源中执行）
| 实验 | 配置 | 结果 |
|---|---|---|
| A 稳定基线 | 默认开关、新卷 | ✅ readiness 3s；invite/join 200；62s×2 探针 16/16 全 200；日志 delete/forget/unknown=0 |
| B 容器重启（同卷） | 同 A | ✅ 原 token 有效、旧房在 joined、旧/新房 state 200 |
| D 双开关 false | 新卷新容器 | ✅ 32/32 探针全 200（含 invite/join 200） |
| C daemon 重启后 | A 资源恢复 | ✅ 自启、原 token/旧房/新房全 200 |

## 决定性异常（C 之后持续）
- 对 p7g-tuwunel-a1（唯一 172.22.0.2 占用者）：**createRoom=200 但 /rooms/{id}/state+invite 6/6 = 404 M_UNRECOGNIZED**（单进程连发，无 flap）
- 对 p7h-tuwunel-a（重启前 16/16 良好实例）：VM 重启后**同样** register/create 200 但 /rooms/* 404 M_UNRECOGNIZED
- → 排除：DELETE/FORGET 配置（D 通过）、RocksDB 卷（B 通过）、实例切换（cid 恒定）、tuwunel 本体（重启前全绿）
- → 归因：**WSL/daemon 重启生命周期或恢复时序**（VM 重启后 tuwunel 以某种退化形态启动，room 路由族缺失；非房间路由 grep 级证据：`docker logs` 无 delete/forget；server_name/room version 无差异）

## 下一步（一条命令可判）
`wsl --terminate MergePilot-Test` 完整冷启动 → 立即重跑鉴别器（register/create/state）：
- 冷启动后路由恢复 → 根因=非清洁恢复时序；操作规程=「先起 tuwunel 再验路由再 start」，直接冲 READY
- 冷启动后仍复现 → 升格 tuwunel 上游问题（携本文件问维护者）


## 7.3I 现状（BLOCKED_CONFIGURATION 收敛中）
tuwunel(p7i)运行+9身份+token 全就绪；卡点=【membership 探针视角 vs 发布通道】映射——读 cmd_start 的 transport 注入 10 行定 vantage，room_id/credentials 指回 p7i 文件后重跑 start。

## FINAL（7.3I2）
BLOCKED_CONFIGURATION。Matrix(p7i) 健康、9 身份、契约房 5/5；两探针未过：membership=本轮配置写入瞬断后仍指死房间（staged 修复脚本就绪）；docker_gw_priority=CLI False 与 doctor True 间歇（门禁保留，未绕过）。transport 通用修复已交付并接线（初始探针与 lifecycle provider 同 vantage）。

## 7.3I3 FINAL → BLOCKED_RUNTIME_MATRIX_TRANSPORT
可路由房 !wUhWu：GET 200/200，同秒窗 POST invite=404 M_UNRECOGNIZED、GET members 翻转空——动词级不对称实锤，传输/发布层存在第二响应者。四 Agent 不可达，Phase 7 以此收卷。

## 7.3I3 终态 → BLOCKED_CONFIGURATION
门禁梯子连续突破：membership ✓（transport+p7i 房）→ gw_priority ✓（预热重试）→ Ubuntu-22.04 docker 启动 ✓ → 当前 typed 首败=E2E_ROLE_TOKEN_EXTRACT_FAILED（reviewer 网关令牌缺失，属 M8-GH hiclaw 角色资产未供给）。Phase 7 以 BLOCKED_CONFIGURATION 完整收卷；模型调用 0、四 Agent 未触发、零伪造。
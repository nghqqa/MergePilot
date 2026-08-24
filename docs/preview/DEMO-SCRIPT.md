# MergePilot v0.1 Preview — 5–8 分钟演示脚本

版本 v0.1.0-preview.1 · 演示环境 = 本地 staging（loopback-only）
**开场即读三句硬标注**：`transport_profile=wsl-user-relay`；
`direct_routing_verified=false`（经中继）；五项真实性边界全部
false / NOT_VERIFIED——控制台是发布管道，不构成应用集成，不代表生产验证。

## 0:00–0:30 环境与启动（若栈已起可跳过）

```
powershell ... bootstrapper.ps1 -Action Check     # 五项环境检查全绿
powershell ... bootstrapper.ps1 -Action Status    # healthy
```
打开 http://127.0.0.1:8600/e2e-status.html。一句台词：**"这是一个只读的
E2E 运维控制台——没有 apply、没有 delete，任何按钮都不存在写操作。"**

## 0:30–2:00 live 态：诚实呈现"没有的东西"

当前 live 投影是 `run-showcase-a`（github_e2e=false 的最小投影）：
- 裁决 **未知态**，所有 E2E 字段 **未提供**，时间线显示"时间线未提供（旧版投影字段缺失）"；
- 右栏五项边界整齐排着 **NOT_VERIFIED**；
- 台词：**"缺什么就说什么——没有的字段绝不合成，未验证的边界绝不装绿。"**
- 指着应用栏的"更新于 … · N 秒前"：年龄读的是**投影自身**的时间戳，冻结的数据会如实变旧。

## 2:00–3:30 complete 态：run35 真实证据

挂载 run35 真实投影（`docs/preview/projections/complete.run35.json`）：

```powershell
copy docs\preview\projections\complete.run35.json .mergepilot\public\status.json
# 页面 10 秒内自动刷新，或点"刷新"
```

- 裁决 **Complete**，Stage=complete，前置 16 项，receipt/matrix=verified；
- **17 阶段时间线**：全绿通过，逐行带着真实证据数（16 项 / 6 边 / 11 服务）；
- **六边路由矩阵**：逐边 VERIFIED——指着"直连路由 **false（经中继）**"：
  **"边探测是逐边验证的；但直连路由声明是 false，因为走的是 wsl-user-relay 中继，控制台绝不把它说成 VERIFIED。"**
- 五项边界依然 NOT_VERIFIED——**"一次成功的 E2E 运行不翻转任何真实性边界。"**

## 3:30–4:30 failed 态：30 秒定位故障

挂载 `projections/failed.fixture.json`（由生产代码 public_status_payload
从失败会话字典真实派生，非手写数据）：

- 裁决 **Failed**；时间线 1–9 绿、**第 10 行红**并内嵌错误框
  `E2E_ROUTE_PROBE_FAILED + 中文解释`；11–17 变灰"待执行"；
- 路由表 `proxy-b-to-winproxy` 一行 **FAIL**，其余 VERIFIED；
- 页底置顶"首个稳定错误"横幅；
- 台词：**"维护者 30 秒内拿到：失败阶段、稳定错误码、哪条边挂了、中继资源归属（3 容器/3 单元/0 探测）。"**

## 4:30–6:00 stale 态：坏消息也要诚实

另开终端停掉上游（演示资源，随时可恢复）：

```powershell
wsl -d MergePilot-Test -u root -- docker stop mergepilot-isolated-demo-console-1
```

- ~30 秒后裁决变 **陈旧**（琥珀），aria-live 播报"运行状态：陈旧"，
  **最后良好数据原样保留**；
- 重启上游 `docker start mergepilot-isolated-demo-console-1`，一个刷新周期内
  自动恢复 **Complete**；
- 台词：**"上游失联不装死也不清屏——保住最后的真相，并明确告诉你它旧了。"**
- 演示完把 live 投影还原（staging 自身状态）或保持 complete 均可，见 PROJECTIONS.md。

## 6:00–7:00 移动端与只读收尾

- 浏览器 DevTools 切 390px：应用栏收拢、路由表变可展开边卡片、无横向滚动；
- 页脚一句话：**"Read-only —— 本控制台不提供任何写操作。"**
- 负向三连（可选 30 秒）：
  `curl -X POST .../api/e2e/status` → **405**；未知路径 → **404**；
  `curl -H "Host: evil.com" .../e2e-status.html` → **403**。

## 计时表

| 段 | 内容 | 时长 |
|---|---|---|
| 1 | 环境与启动 | 0:30 |
| 2 | live 未知态/未提供/边界 | 1:30 |
| 3 | complete + run35 证据 + false（经中继） | 1:30 |
| 4 | failed 定位 | 1:00 |
| 5 | stale 与自愈 | 1:30 |
| 6 | 移动端 + 只读收尾 | 1:00 |

合计 ≈ 7 分钟；赶时间可跳过第 6 段压到 6 分钟。

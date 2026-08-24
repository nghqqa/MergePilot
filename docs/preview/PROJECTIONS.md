# 三种真实投影说明（complete / failed / stale）

版本 v0.1.0-preview.1 · 三份 JSON 均由**生产派生代码**生成，非手写数据。

## 投影从哪来

唯一写者 `write_session`（`tools/cli/mergepilot.py`）在 journal 每次持久化时
调用 `public_status_payload(session)` 派生白名单投影，写入
`.mergepilot/public/status.json`，由 demo-console **只读挂载**后经
`/api/e2e/status` 原样服务。演示时可把任一投影文件复制到该路径（复制前
请备份当前 live 文件——见文末"还原"）。

## 1. complete.run35.json — 真实运行证据

- 来源：`b8-e2e-run35` 的完整真实 journal（Stage 1–17 全真实通过，
  GITHUB_E2E_COMPLETE），逐字节为单写者当时的输出；
- 关键字段：`e2e_stage=complete`、`journal_complete=true`（严格相等）、
  前置 `checks_passed=16`、`receipt_verified/matrix_verified=true`、
  六边 `route_probes` 逐边 `verified=true`；
- **硬标注**：`transport_profile=wsl-user-relay`、
  `direct_routing_verified=false`（页面显示 `false（经中继）`）；
- `truth_boundaries` 五项全部 `NOT_VERIFIED`
  （application_integration_verified / database_verified /
  production_verified / revision_producer_contract /
  audit_producer_contract）——成功运行不翻转任何边界。

## 2. failed.fixture.json — 生产代码派生的失败演示

- 来源：以失败会话字典（`e2e_stage=route_probes`、
  `e2e_last_error={code: E2E_ROUTE_PROBE_FAILED, stage: route_probes}`、
  `proxy-b-to-winproxy` 边 verified=false）调用**生产函数**
  `public_status_payload()` 生成——派生路径与线上完全一致，仅输入是演示字典；
- 页面表现：裁决 Failed、时间线第 10 行红并内嵌稳定错误码、
  11–17 待执行、路由表一行 FAIL、页底"首个稳定错误"横幅；
- `e2e_last_error` 字段在生产中由 `_fail()` 于回滚前写入 journal
  （尽力持久化，persist 失败绝不阻断回滚）——首个错误获胜，后续不覆盖。

## 3. stale — 行为态，无文件

stale 不是一种投影，而是**呈现层对失联的诚实反应**：

```
wsl -d MergePilot-Test -u root -- docker stop mergepilot-isolated-demo-console-1
# ≥30 秒（STALE_MS）后：裁决→陈旧，aria-live 播报，最后良好数据保留
wsl -d MergePilot-Test -u root -- docker start mergepilot-isolated-demo-console-1
# 一个刷新周期（≤10 秒）内自动恢复真实裁决
```

判定语义：陈旧 = **连续刷新失败超过 30 秒**（网络/上游失联），
与投影内容新旧无关；后者由应用栏的投影年龄（`updated_utc` 距今）如实表达。

## 还原 live 状态

live 会话（`run-showcase-a`，github_e2e=false 最小投影）已存档于
`live.showcase.json`；演示结束把它复制回
`.mergepilot/public/status.json` 即恢复 staging 诚实基线。
下一次 `mergepilot start` 也会由单写者重新生成。

## 秘密与安全

三份投影均经扫描：零 PAT/PEM/Matrix token/MinIO credential；
白名单结构上不含任何秘密邻接字段。

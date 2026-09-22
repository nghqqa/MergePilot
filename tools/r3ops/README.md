# r3ops — 决赛冲刺轮运维工具集（r3work 回流）

从本地工作区 `D:\goai\r3work\scripts\` 精选回流的运维脚本，覆盖 WH 收官轮全链路的操作面。
历史中间版本（sk2/sk3/sk4/v3/rag/r2t 各变体）未回流，其运行产物见 `evidence/` 对应包。

## 脚本清单

| 脚本 | 用途 |
|---|---|
| `matrix.py` | Matrix HTTP API 辅助库（token/发送），所有 kickoff 脚本的公共依赖 |
| `send_kickoff_pr{1,2,3}sk5.py` | SK5/收官轮三案例的 Leader kickoff（PR1 低风险自动 / PR2 批准 / PR3 拒绝） |
| `send_gate_decision.py` | 人工门决策投递（含占位 head SHA 拒收守卫——见 CORRECTIONS 勘误第 3 条） |
| `monitor.py` | 运行监控 |
| `enable_otel.py` | 向运行中的 worker 激活 OTel（mc pipe 写激活配置） |
| `otel_direct_probe.py` | OTel 直连探针 |
| `kick_link.py` | 最小链路验证 kickoff（ack-only） |
| `repro_wedge.py` | reviewer 冷启动卡死复现器（WORKING_DIR import 竞态，见 entrypoint-fix） |
| `package_wh_evidence.py` / `package_wh2_evidence.py` | WH 轮证据打包（含台账空清单 fail-loud 断言） |
| `gen_timeline_svg.py` | PR2 协作时间线 SVG 生成（`docs/assets/pr2-collaboration-timeline.svg` 的来源） |

## 相关位置

- 桥主程序：`tools/gh-bridge/`（gh_bridge.py + startup_fullchain.ps1）
- 镜像构建：`docker/Dockerfile.v4boot`、`docker/Dockerfile.v3skills`
- Workflow Controller（产品化方向）：`tools/workflow-controller/`

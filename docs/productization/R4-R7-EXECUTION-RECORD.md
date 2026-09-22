# R4 + R7 执行记录（EXECUTION RECORD）

**日期**：2026-09-22 ｜ **授权**：用户批准 R4、R7（附八项执行条件，已全部遵守）｜ **结果**：✅ 两项完成，零回退，零外部写入，零 R1/R2/R3 操作

## R4 运行副本同步（已完成）

**步骤 1–2 备份与摘要**（同步前）：

| 文件 | sha256（同步前） | 备份 |
|---|---|---|
| `r3work\scripts\gh_bridge.py`（旧运行副本） | `c0c53c74…3d65` | `gh_bridge.py.bak-20260922-204703` |
| `r3work\rag-live\rag-live-corpus.json` | `c83d8e83…10ed` | `….bak-20260922-204703` |
| `r3work\rag-live\rag-live-server.mjs`（旧版，未改动） | `34ded5e4…1af9dd` | `….bak-20260922-204703` |

**步骤 3 同步清单**（12 文件，全部 sha256 双侧一致）：

- `r3work\scripts\gh_bridge.py` ← `tools/gh-bridge/gh_bridge.py`（`840b193c…6be8`）
- `r3work\orchestrator\`（10 个 py：adapter/runstore/risk/stages/scheduler/aggregate/verify_finding/console_contract/flag/__init__）
- `r3work\rag\corpus_tool.py` ← `tools/rag/corpus_tool.py`

**步骤 4 off 冒烟（MERGEPILOT_REVIEW_V3=off，全部通过）**：

1. `python gh_bridge.py status`（同步副本，只读 SELECT 服务器台账）→ exit 0；
2. adapter 从 **r3work\orchestrator** 加载成功（非 repo）；
3. off 模式 hook 调用：零日志、`~/.mergepilot/v3-runs.db` 未创建/未改动；
4. shadow 能力在位且诚实：注入 diff 不可得 → `risk=None`、`external_writes=none`（临时目录存储，未污染）。

**旧链路行为不变结论**：桥进程此前未运行；同步不改变任何在跑进程。下次启动的现网桥 = 加固版，默认 off = 对外行为与旧链路一致（台账语义、kickoff、回写全部未改，v3 只在派发边界追加只读证据且可关）。

## R7 语料同步 + rag-live（已完成，服务按条件已停止）

| 步骤 | 结果 |
|---|---|
| 语料幂等导入 | `changed=false`（运行语料与 repo 语料本就同字节），snapshot_id=`fd34c304…1afa4`，chunks=12 |
| 部署核对 | repo 与运行语料原始字节 sha256 均为 `c83d8e83…10ed`，match=true |
| 启动（repo 事实源路径） | `node tools/rag/live/rag-live-server.mjs`，RAG_LIVE_PORT=4184，运行语料/审计路径 env 指向 r3work |
| /health | ok=true，chunks=12，data_mode=SYNTHETIC，`corpus_file_sha256=c83d8e83…10ed`（与部署文件一致） |
| 本地检索验证 | 查询 "CWE-22 路径穿越 path traversal" → 命中 `doc-cwe22-def#1`(14.25) + `doc-path-containment#2`(9.16)，source_refs 正确 |
| run_id 透传 | `run_id=r4r7-smoke-1` 进入审计记录（含 corpus_file_sha256）——R5 接通 MCP 透传后 run 关联即闭合 |
| 快照一致性 | 同步副本桥 `_rag_snapshot_info()` → `fd34c304…1afa4` == manifest 绑定值 == corpus_tool 输出 |
| 服务停止 | 按条件 5 停止（pid 216304 killed，:4184 无监听确认）；启动命令已记录（见下） |

## 回退命令（已验证可行，未需要执行）

```
copy /Y "D:\goai\r3work\scripts\gh_bridge.py.bak-20260922-204703" "D:\goai\r3work\scripts\gh_bridge.py"
rmdir /S /Q "D:\goai\r3work\orchestrator"
rmdir /S /Q "D:\goai\r3work\rag"
copy /Y "D:\goai\r3work\rag-live\rag-live-corpus.json.bak-20260922-204703" "D:\goai\r3work\rag-live\rag-live-corpus.json"
```
（rag-live 本轮从 repo 路径启动，r3work 服务文件未被改动；停止命令：结束监听 :4184 的 node 进程。）

## 边界声明

- 未执行 R1/R2/R3 的任何操作；无 GitHub 写入；无共享环境故障注入；模型调用零消耗。
- `MERGEPILOT_REVIEW_V3` 保持 off；现网下次启动的桥行为与旧链路一致（除只读 v3 证据，可关）。
- rag-live 已停止；重新启动命令：`cd D:\goai\MergePilot && set RAG_LIVE_PORT=4184 && set RAG_LIVE_CORPUS=D:\goai\r3work\rag-live\rag-live-corpus.json && set RAG_LIVE_AUDIT=D:\goai\r3work\rag-live\rag-tool-spans.jsonl && node tools\rag\live\rag-live-server.mjs`

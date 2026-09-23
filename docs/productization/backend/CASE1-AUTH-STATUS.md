# CASE1 授权状态与待批缺口

**基线** `903e995` ｜ **日期** 2026-09-23 ｜ **状态：全部 BLOCKED，等待用户逐项批复**

## 一、授权状态矩阵

| # | 事项 | 状态 | 阻塞原因 |
|---|---|---|---|
| R4 | 运行副本同步 | **BLOCKED** | 用户未写"批准" |
| R7 | rag-live 启动 | **BLOCKED** | 用户未写"批准" |
| R1-lite | 空提交触发 | **BLOCKED** | 用户未写"批准"；涉及 Git push（外部写入） |
| R2-lite | check-run 发布 | **BLOCKED** | 用户未写"批准"；不可逆外部写入，须单独获批 |
| R3 | 故障注入 | **默认拒绝** | 用户未要求；本轮不开 |
| D-1 | 审批动作集 | **BLOCKED** | 用户未确认推荐值 |
| D-2 | 审批人映射 | **BLOCKED** | 用户未提供具名主体 |
| D-3 | TTL | **BLOCKED** | 用户未确认 24h |
| D-4 | 预算硬上限 | **BLOCKED** | 用户未提供数值或确认兜底方案 |
| D-6 | 凭证轮换 | **BLOCKED** | 挂起中，用户未决定"现网可用"或"先轮换" |
| reviewer/leader 停止 | **BLOCKED** | 需与 R1-lite 一起批复 | |

## 二、运行副本精确差量（2026-09-23 只读核实）

### gh_bridge.py

| 侧 | sha256 前 16 |
|---|---|
| repo (`tools/gh-bridge/`) | `bb1fafaedf5c2679` |
| r3work (`scripts/`) | `840b193ce24a2e23` |

**差量内容**：repo 版含 M1 全量加固 + manifest + RAG 门 + 定向认领 + 发布五分类。
同步后需 off 冒烟确认行为不变。

### orchestrator/

| 文件 | 状态 |
|---|---|
| `__init__.py` | MATCH |
| `adapter.py` | MATCH |
| `aggregate.py` | MATCH |
| `console_contract.py` | MATCH |
| `flag.py` | MATCH |
| **`pg_runstore.py`** | **MISSING in run copy** |
| `risk.py` | MATCH |
| `runstore.py` | MATCH |
| `scheduler.py` | MATCH |
| `stages.py` | MATCH |
| `verify_finding.py` | MATCH |

### RAG 语料

| 侧 | sha256 前 16 |
|---|---|
| repo (`tools/rag/corpus/`) | `c83d8e8383ab1609` |
| run (`r3work/rag-live/`) | `c83d8e8383ab1609` |

**语料已同步**（无需操作）。

### rag-live 服务文件

| 侧 | sha256 前 16 |
|---|---|
| repo (`tools/rag/live/`) | `c73691b4e03d2800` |
| run (`r3work/rag-live/`) | `34ded5e4618acd95` |

**差量**：repo 版含 `corpus_file_sha256` /health 字段与 `run_id` 审计透传（R5 增量）。

## 三、同步计划（待 R4+R7 批复后执行）

```bash
# 备份
copy D:\goai\r3work\scripts\gh_bridge.py D:\goai\r3work\scripts\gh_bridge.py.bak-%date%
# 同步
copy tools\gh-bridge\gh_bridge.py D:\goai\r3work\scripts\gh_bridge.py
# 补齐 orchestrator 子集（运行副本新增）
mkdir D:\goai\r3work\orchestrator
copy tools\orchestrator\*.py D:\goai\r3work\orchestrator\
# off 冒烟
set MERGEPILOT_REVIEW_V3=off
python D:\goai\r3work\scripts\gh_bridge.py status
# 回退
copy D:\goai\r3work\scripts\gh_bridge.py.bak-%date% D:\goai\r3work\scripts\gh_bridge.py
```

## 四、rag-live 启动命令（待 R7 批复后执行）

```bash
set RAG_LIVE_PORT=4184
set RAG_LIVE_CORPUS=D:\goai\r3work\rag-live\rag-live-corpus.json
set RAG_LIVE_AUDIT=D:\goai\r3work\rag-live\rag-tool-spans.jsonl
node tools\rag\live\rag-live-server.mjs
# 验证
curl http://127.0.0.1:4184/health
# 停止：结束 node 进程
```

## 五、预算与凭证缺口

| 项 | 现状 | 需要用户做什么 |
|---|---|---|
| provider 预算硬上限 | **未设置** | 在 gateway 上游 key 的 provider 控制面设置消费/频率配额 |
| 本地运行截止 | 已实现 | 桥 `--timeout-min 20`（代码内） |
| 人工停止信号 | 可用 | kill 桥进程 → stop leader → stop reviewer |
| 在途请求 | **可能继续计费** | 进程退出≠计费停止——如实告知，不伪装 |
| 无 provider 硬上限时 | **预算不受控** | 不得写"预算受控"或"硬上限成立" |

## 六、凭证状态

| 项 | 现状 |
|---|---|
| GitHub App installation token | 每次报告器新进程自动获取（checks:write）；未轮换 |
| gateway 上游 key | 共享凭证（非本案例专用）；未轮换 |
| WEBHOOK_SECRET | 未轮换（receiver HMAC 校验用；部署一致即可工作） |
| 轮换状态 | **挂起中（用户决定 D-6）**；不自行执行 |

## 七、取消范围（待批复后才可执行）

取消顺序（设计已实现，真实执行待 R1 授权）：
1. **先阻止 leader**（防再委托）
2. **再停止 reviewer**（切断模型调用）
3. fixer/verifier 空闲（无需停止）
4. **在途上游请求可能仍计费**（进程退出≠费用停止）

reviewer-only stop **不是**完整费用切断——leader 可能仍在产生编排调用。

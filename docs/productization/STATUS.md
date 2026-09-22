# MergePilot 产品化推进状态（STATUS）

**更新**：2026-09-22（第三轮：RAG 审计与接入闭环）｜ **分支**：`chore/backfill-r3-ops` ｜ 授权范围：M1→M4 至 V0 技术就绪

## 本轮新增：RAG 审计 + V0 最小闭环（详见 RAG-AUDIT.md / ACCEPTANCE-RAG）

| 工作项 | 状态 | 说明 |
|---|---|---|
| RAG 全链路审计 | ✅ 完成 | RAG-AUDIT.md：A 知识链（rag_retrieve→rag-live BM25→org 标准语料）**已接线且有历史运行/消费证据**（64 次调用、result.md 实际引用）；当前服务未运行；失败语义与版本绑定缺失 |
| 语料事实源入库 + 快照版本 | ✅ 实现+测试 | tools/rag/corpus/（snapshot fd34c304…，与运行副本实测同字节）+ corpus_tool（内容寻址/幂等导入/原子更新）+ live/ 服务事实源入库 |
| 桥派发绑定 RAG 快照（RAG-4） | ✅ 实现+测试 | manifest.rag.snapshot_id/chunks/data_mode/service_state_at_dispatch/policy |
| RAG_REQUIRED 派发门（RAG-6） | ✅ 实现+测试 | 默认 advisory（现状语义，降级经 manifest 可见）；=1 时快照不可读或服务不可达→拒派发 ERROR fail-closed |
| 隔离集成测试（第 2 层） | ✅ 12 测试 | 真实 rag-live-server.mjs 进程：已知命中/合法空/审计链/快照一致/注入语料形状/不可达/变更即新快照 |
| 票据存储 P-1 | ✅ 已决策+实现 | **SQLite WAL**（推翻 MinIO 提案）：跨连接竞争 CAS/崩溃重开/partial UNIQUE 幂等，7 测试；见 DECISIONS P-1 |
| 预算重试语义 | ✅ 复查+固化 | 预留去重≠成本去重，累计结算契约入测试（DECISIONS #11） |

## 阻塞重新分类（取代上轮"全部待拍板"口径）

| 项 | 分类 | 处置 |
|---|---|---|
| P-1 票据存储 | B 普通工程选择 | **本轮已自主决策并实现**（SQLite WAL，可逆，不涉共享环境） |
| D-1 审批动作集 / D-2 审批人 / D-3 TTL | D 真业务决策 | 机制已全部实现且拒绝未配置状态（approved_by 非空强制、动作集白名单、TTL 参数化）；**启用**仍需用户定 |
| 预算金额 | D 真业务决策 | 接口/持久化/预留/结算/并发/崩溃全部已实现+测试；未配置不产生任何消费授权 |
| R1-R4（真实集成轮） | C/D 外部授权 | 待批；每场景独立触发条件与判据见 INTEGRATION-AUTH-REQUESTS，**不预设一轮完成全部 M1 验收** |
| R5 镜像层改动 | C 需授权（worker 重启） | 只读探查已完成（RAG/manifest 已受益）；镜像改动待批 |
| R6 usage 源 | C 需选路+权限 | 二选一待用户选 |
| R7 语料部署同步 | C 需授权（本地部署操作） | 已列清单：备份/导入/回退齐备 |
| 密钥轮换 | D 用户挂起中 | M4 出口条件 |

## 里程碑真实状态

- **M1**：单测级收口 + 契约完备；场景 3/6/7/9 的**真实环境实证未做**——未通过，不因桥加固冒充接管。
- **M2**：规格+纯逻辑+SQLite 存储+隔离测试完成；门 Web 页、真实审批主体与动作启用未做——未通过。
- **M3**：未开始（污染测试需真实 run，待授权）。
- **M4**：RAG 第 1/2 层达成（第 3/4 层待授权）、成本接口层达成、版本清单达成；**V0 技术就绪未达成**，真实内测未开始。

## 下一条可执行动作（按序）

1. **门 Web 页骨架（只读展示票据）**：消费 SQLite 票据库的只读页 + 签发 API 骨架（本地、不接真实审批主体，D-1/D-2 拍板前不可放行真实动作）；
2. **B 案例链零命中排查**：skill_case_retrieval 8 次调用全 document_count=0——查 case-pg 数据时间线与查询形状（只读，可自主）；
3. **待授权批复后**：R7（语料同步）→ R1+R2+R4 合并真实案例轮 → R3 双 head。

## 已验证结果（证据，2026-09-22 实测 @ 工作树）

- `python -X utf8 -m pytest tests/gh_bridge/ tests/rag_live/ tests/approval/ tests/costmeter/ -q` → **121 passed**（bridge 46 + rag_live 20 + approval 41 + costmeter 17；2.9s）
- `python -X utf8 -m pytest tests/gh_app/ -q` → 816 passed, 5 skipped（上轮实测；本轮改动不触其引用面）
- RAG 双副本快照实测一致：repo 与 r3work 语料 snapshot_id 同为 fd34c304…
- M1 九场景：1/2/3/4/5/7/8 ✅单测；9 ✅契约+单测；6 🔒结构保证（实证待授权）
- gh_app "831" 勘误见前版记录（821 collected 为准）

## 当前阻塞（外部条件）

- R1-R4/R5/R6/R7 真实环境授权与选择；D-1/D-2/D-3 产品决策；预算金额；密钥轮换（用户挂起）。

## 提交记录（本轮新增，均未 push）

- 本轮 RAG+P-1 提交（见 git log）：RAG 审计/语料入库/桥快照门/隔离集成测试 + SQLite 票据存储 + 预算语义

## 环境事实（2026-09-22 实测）

- 本地栈 8 容器运行中；**rag-live 未运行**（netstat 4174/4184 无监听）；worker 镜像内 rag_mcp hook 最近激活 2026-09-21。
- 桥运行副本 `D:\goai\r3work\scripts\gh_bridge.py` **本轮未同步**（repo 领先：M1 加固+manifest+RAG 门，待 R4/R7 授权后按备份/回退规程同步）。

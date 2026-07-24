# MergePilot 原型搭建 · Team 重建 Runbook

> 这是把 MergePilot 多 Agent 团队在 HiClaw 上重建的**权威步骤手册**(含踩过的坑)。
> 复赛重建、Demo 环境重置、或新机器复现,都照这个走。
> 验证日期:2026-07-23(首轮 Team 路径与全 OpenClaw 双场景路径均有证据,见 `D:\goai\evidence\`)。
>
> 当前保留两条可复现路径:原 Team 路径用于复现首轮 40/40 与 18 份证据；全 OpenClaw 路径由系统 Manager 直接编排 reviewer/fixer/verifier，用于复现 PR #42 / #43 双场景。两条路径都属于 AgentTeams 协同验证。

---

## 0. 前置条件

- HiClaw 已装通(见 `docs\环境搭建-HiClaw-WSL.md`):WSL2 + Docker、DeepSeek(`deepseek-v4-flash`)配好、Element Web 能登录。
- 4 份 SOUL.md 就位:`D:\goai\workers\{coordinator,reviewer,fixer,verifier}\SOUL.md`
- Team 配置:`D:\goai\workers\team.yaml`

---

## 1. 核心认知(决定成败,先理解)

1. **多 Agent 协作必须用 Team 机制**。HiClaw 的 Team Leader 有框架自带的**委派工具**(任务拆解 + 路由到 team worker + 共享房间),这是 standalone worker 做不到的——standalone worker 互相 @mention 触发不了对方(LLM 发的是文本,不是 Matrix 真·提及胶囊)。
2. **原 Team 路径的 coordinator leader 与系统 Manager 是两个不同角色**。首轮证据使用 coordinator leader；新增双场景使用系统 Manager(OpenClaw)直接编排 3 个独立 Worker。不要把两条路径的运行时结论混写。
3. **team 成员的配置只能通过 team API 改**(`hiclaw apply -f team.yaml`),`hiclaw update worker` 对 team 成员无效。

---

## 2. 重建步骤

### Step 1 · 清空同名 standalone worker(若有)

`create team --workers <已存在的名字>` 会建重复 worker 并级联删容器,所以**先删干净**:
```bash
wsl -- bash -c '
docker exec hiclaw-controller hiclaw delete worker coordinator
docker exec hiclaw-controller hiclaw delete worker reviewer
docker exec hiclaw-controller hiclaw delete worker fixer
docker exec hiclaw-controller hiclaw delete worker verifier
'
```
(保留 `alice` 无所谓,它是通用测试 worker。)

### Step 2 · 建 Team(让 4 个 worker 随 team 一起创建)

```bash
wsl -- bash -c '
docker cp /mnt/d/goai/workers/team.yaml hiclaw-controller:/tmp/team.yaml
docker exec hiclaw-controller hiclaw create team \
  --name mergepilot --leader-name coordinator --leader-model deepseek-v4-flash \
  --workers reviewer,fixer,verifier \
  --description "PR review/fix/verify closed loop"
'
```

### Step 3 · 补 model(team 建的 worker 默认 model 为空)

```bash
wsl -- bash -c '
docker exec hiclaw-controller hiclaw apply -f /tmp/team.yaml
'
```
(team.yaml 里每个 worker 都显式写了 `model: deepseek-v4-flash`;apply 后全部补齐)

### Step 4 · 注入 4 份 SOUL

```bash
wsl -- bash -c '
docker cp /mnt/d/goai/workers/coordinator/SOUL.md hiclaw-worker-coordinator:/root/hiclaw-fs/agents/coordinator/SOUL.md
docker cp /mnt/d/goai/workers/reviewer/SOUL.md   hiclaw-worker-reviewer:/root/hiclaw-fs/agents/reviewer/SOUL.md
docker cp /mnt/d/goai/workers/fixer/SOUL.md      hiclaw-worker-fixer:/root/hiclaw-fs/agents/fixer/SOUL.md
docker cp /mnt/d/goai/workers/verifier/SOUL.md   hiclaw-worker-verifier:/root/hiclaw-fs/agents/verifier/SOUL.md
docker exec hiclaw-worker-reviewer hiclaw-sync
docker exec hiclaw-worker-fixer hiclaw-sync
docker exec hiclaw-worker-verifier hiclaw-sync
# copaw 容器没有 hiclaw-sync 命令,coordinator 跳过即可(SOUL 文件在路径上,它会读)
'
```

### Step 5 · 验证

```bash
wsl -- docker exec hiclaw-controller hiclaw get teams     # mergepilot Active, 3/3 Ready
wsl -- docker exec hiclaw-controller hiclaw get workers   # 全 Running, deepseek-v4-flash
```

---

## 3. 用法 A:原 Team 路径(首轮基准)

1. **派任务(推荐:走 Manager 路由,最稳)**:
   ```bash
   MSYS_NO_PATHCONV=1 wsl -- bash -c 'docker cp /mnt/d/goai/tools/submit_pr_manager.py hiclaw-manager:/tmp/ && docker exec hiclaw-manager python3 /tmp/submit_pr_manager.py <admin_password>'
   ```
   把 PR 发给**系统 Manager**,Manager(openclaw,官方入口)可靠接收 → 建 task → 路由给 mergepilot team 的 coordinator → 自动拆 review→fix→verify。
   > ⚠️ **别直戳 coordinator DM**:copaw 的空闲消费者清理 + 会话状态导致直 DM 重复触发不稳(已实测)。**走 Manager 才稳**(已验证完整闭环跑通)。
   > (手动方式:在 Element 里给 `manager` 发"请让 mergepilot team 处理这个 PR: [代码]";**不要**直接 DM coordinator。)
2. **自动编排**:coordinator 自动拆成 review→fix→verify 的 DAG 并调度三个 worker(无需手动 @)。
3. **审批门**(L2 高危):coordinator 会出审批待办。在 coordinator 房间回复:
   > 作为 Team Admin,确认批准 F-XXX 的修复部署。请 fixer 应用 → verifier 复核 → 合并。
   → 触发 fixer 执行生产补丁 + verifier 最终复验 + 合并。
4. **取证据**:产物在 MinIO `shared/tasks/<project-id>-{01..05}/`,拉取:
   ```bash
   wsl -- bash -c '
     for n in 01 02 03 04 05; do :; done   # 循环变量在 wsl 层会丢,用显式 5 条:
   '
   # 显式(可用):
   wsl -- docker cp hiclaw-manager:/root/hiclaw-fs/shared/tasks/<project-id>-01 /mnt/d/goai/evidence/
   # ... -02 -03 -04 -05 同理
   ```
   (注意目录名带 `-NN` 后缀,不是单一父目录)

## 4. 用法 B:全 OpenClaw 双场景路径

1. 安装 AgentTeams v1.1.2 时将系统 Manager 运行时设为 OpenClaw，并确保 reviewer/fixer/verifier 3 个独立 Worker 均为 OpenClaw。
2. Demo 前可运行 `bash tools/demo_prepare.sh` 重启 3 个 Worker，减少上下文粘滞对复现的影响。
3. 将编排脚本复制进 Manager 容器并提交任务:
   ```bash
   MSYS_NO_PATHCONV=1 wsl -- bash -c 'docker cp /mnt/d/goai/tools/submit_manager_orchestrate.py hiclaw-manager:/tmp/ && docker exec hiclaw-manager python3 /tmp/submit_manager_orchestrate.py <admin_password>'
   ```
4. PR #42 / #43 的原始 findings、fix-plan、verify-report 与 trace 位于 `evidence/mergepilot-openclaw-run/`。PR #43 最终记录为 `HOLD / Do not merge`，不是已执行 `REJECT`。

> 在当前 v1.1.2 本地环境中，OpenClaw Manager 的消息处理更可靠；该结论不外推为所有版本和部署的强制要求。

---

## 5. 踩坑记录(别重蹈)

| 坑 | 现象 | 对策 |
|---|---|---|
| `create team --workers <已存在>` | 建重复 worker + 级联删原容器 | 先 delete 干净再 create team |
| team 成员改不了 | `update worker` 报 HTTP 409 | 用 `apply -f team.yaml` 改 |
| Team coordinator 的 runtime 字段未按预期生效 | 原 Team 路径中 yaml runtime 被忽略 | 保留首轮路径；双场景改用 OpenClaw 系统 Manager 直接编排独立 Worker |
| specialist model 空 / Pending | team 建的 worker 没继承 model | apply team.yaml 补 model |
| copaw 没 hiclaw-sync | coordinator SOUL 注入后 sync 报错 | 跳过,文件在路径上即可 |
| `for n in ...` 在 `wsl -- bash -c` 里 | 循环变量被吞($n 空) | 用显式多条命令,别用循环 |
| 群房间 @mention 不触发 | 纯文本 @ 无效 | Element 里 `@`+首字母选胶囊;但 team 内部调度不靠手动 @ |

---

## 6. 当前实例状态(2026-07-23)

- Team `mergepilot`:Active,3/3 Ready
- coordinator(copaw)+ reviewer/fixer/verifier(openclaw),全 Running,`deepseek-v4-flash`
- 端到端验证通过:review→fix→verify→(L2 审批)→生产补丁→最终复验(40 项检查)全跑通
- 证据产物已拉到 `D:\goai\evidence\`(18 文件)
- 全 OpenClaw 双场景已完成:PR #42 MERGE(人审后)；PR #43 HOLD / Do not merge

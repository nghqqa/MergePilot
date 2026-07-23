# MergePilot 原型搭建 · Team 重建 Runbook

> 这是把 MergePilot 多 Agent 团队在 HiClaw 上重建的**权威步骤手册**(含踩过的坑)。
> 复赛重建、Demo 环境重置、或新机器复现,都照这个走。
> 验证日期:2026-07-22(端到端跑通,证据见 `D:\goai\evidence\`)。

---

## 0. 前置条件

- HiClaw 已装通(见 `docs\环境搭建-HiClaw-WSL.md`):WSL2 + Docker、DeepSeek(`deepseek-v4-flash`)配好、Element Web 能登录。
- 4 份 SOUL.md 就位:`D:\goai\workers\{coordinator,reviewer,fixer,verifier}\SOUL.md`
- Team 配置:`D:\goai\workers\team.yaml`

---

## 1. 核心认知(决定成败,先理解)

1. **多 Agent 协作必须用 Team 机制**。HiClaw 的 Team Leader 有框架自带的**委派工具**(任务拆解 + 路由到 team worker + 共享房间),这是 standalone worker 做不到的——standalone worker 互相 @mention 触发不了对方(LLM 发的是文本,不是 Matrix 真·提及胶囊)。
2. **Team leader 默认 copaw 运行时**,改不动(team.yaml 里 `runtime: openclaw` 被静默忽略;`update worker` 对 team 成员报 HTTP 409)。**但 copaw 能正常用 DeepSeek**,不用管它。
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

## 3. 用法(运行闭环)

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

---

## 4. 踩坑记录(别重蹈)

| 坑 | 现象 | 对策 |
|---|---|---|
| `create team --workers <已存在>` | 建重复 worker + 级联删原容器 | 先 delete 干净再 create team |
| team 成员改不了 | `update worker` 报 HTTP 409 | 用 `apply -f team.yaml` 改 |
| leader 改不了 openclaw | yaml runtime 被忽略 | 别改,copaw 能用 DeepSeek |
| specialist model 空 / Pending | team 建的 worker 没继承 model | apply team.yaml 补 model |
| copaw 没 hiclaw-sync | coordinator SOUL 注入后 sync 报错 | 跳过,文件在路径上即可 |
| `for n in ...` 在 `wsl -- bash -c` 里 | 循环变量被吞($n 空) | 用显式多条命令,别用循环 |
| 群房间 @mention 不触发 | 纯文本 @ 无效 | Element 里 `@`+首字母选胶囊;但 team 内部调度不靠手动 @ |

---

## 5. 当前实例状态(2026-07-22)

- Team `mergepilot`:Active,3/3 Ready
- coordinator(copaw)+ reviewer/fixer/verifier(openclaw),全 Running,`deepseek-v4-flash`
- 端到端验证通过:review→fix→verify→(L2 审批)→生产补丁→最终复验(40 项检查)全跑通
- 证据产物已拉到 `D:\goai\evidence\`(18 文件)

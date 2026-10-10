# 试点部署与生产切换 Runbook（命令级，凭据就位后零决策执行）

- 日期：2026-10-10；依据：#395/#396/#397 组合代码（env 权威清单已按最新代码核实）
- 前置：`D:\goai\secrets\gitee-pilot.env` 三项齐备（TOKEN/WEBHOOK_SECRET/REPO）+维护者已建私有试点仓库

## A. 试点隔离服务部署（生产 rc.22 不触碰）

试点镜像从 PR 分支构建（**非正式制品，不入 Release**；正式镜像在合入后从 main 构建）：

```bash
# 本机：从栈顶分支构建试点镜像
cd /d/goai/mp-worktrees/gitee-integration
docker build -f docker/Dockerfile -t ghcr.io/nghqqa/mergepilot-console:rc23-pilot-candidate .
export HTTPS_PROXY=127.0.0.1:17890; docker push ghcr.io/nghqqa/mergepilot-console:rc23-pilot-candidate
```

VPS（生产同机隔离——独立目录/独立库/独立端口，不共网段不共卷）：

```bash
# 1) 独立库
docker run -d --name mu-pilot-pg -e POSTGRES_PASSWORD=<随机> -e POSTGRES_DB=mu \
  --restart unless-stopped postgres:16-alpine
# 2) 凭据文件（本机 scp，受保护；权限 600）
scp /d/goai/secrets/gitee-pilot.env vps:/opt/mu-pilot/gitee-pilot.env   # → 转换为服务 env（见下）
# 3) 试点服务（独立端口 28600；五项 Gitee env 显式配置）
docker run -d --name mu-pilot-console --link mu-pilot-pg:pg \
  -e MU_MODE=multiuser -e CONSOLE_PG_DSN="postgres://postgres:<随机>@pg:5432/mu" \
  -e CONSOLE_SESSION_SECRET=<新生成，不复用生产> -e CONSOLE_HOST=0.0.0.0 \
  -e MERGEPILOT_VERSION=rc23-pilot \
  -e MU_GITEE_PAT=<TOKEN> -e MU_GITEE_WEBHOOK_SECRET=<SEC> \
  -e MU_GITEE_WEBHOOK_MODE=signature \
  -e MU_GITEE_WEBHOOK_SIGN_SOURCE=header -e MU_GITEE_WEBHOOK_SIGN_ENCODING=url_b64 \
  -e MU_JOB_CONSUMER_ENABLED=1 \
  -p 127.0.0.1:28600:28600 --restart unless-stopped \
  ghcr.io/nghqqa/mergepilot-console:rc23-pilot-candidate
# 4) HTTPS 回调：现有反代新增路由 https://<域名>/pilot/* → 127.0.0.1:28600（TLS 沿用既有证书）
```

**Webhook 精确配置（维护者在 Gitee 仓库设置页执行）**：

| 项 | 值 |
|---|---|
| URL | `https://<域名>/pilot/api/mu/gitee/webhook` |
| 密码/签名 | 签名密钥（SEC 开头，与 MU_GITEE_WEBHOOK_SECRET 同值） |
| 事件 | 仅 **Pull Request** |

**Webhook 实测采集（试点首日，收敛配置用）**：投递后核对——签名在 header 还是 body（`X-Gitee-Token` vs payload `timestamp/sign`）、编码形态（url_b64/b64）、时间戳精度；与 `MU_GITEE_WEBHOOK_SIGN_SOURCE/ENCODING` 配置比对；不一致→改 env 重启（**显式改配，不做自动回退**）→实测结论回填验收记录并冻结配置。

## B. 试点验收序列（全真实 API，结果记入验收记录 §八）

1. probe（GET /user+目标仓库）→连接 valid；私有仓库读取；绑定+稳定 id 核验
2. 开测试 PR→真实 webhook 投递→验真/入队/消费→run 终态+审计
3. 页面手动审查入口→同一消费链
4. 同 head 重触发（幂等）/新 commit（新 run）/同 body 重投（duplicate）
5. 连接撤销→webhook ignored+存量任务 rejected；重登记恢复
6. 维护者正常入口逐票批准→fxv 修复 dry-run→Verifier→终态（真实 Gitee 来源上下文）
7. 全程调用清单审计=纯 GET；远端零写

## C. 正式发布（试点通过+合入后）

1. 版本声明：main 上 `rc.23`（Dockerfile/.env/compose/README/CHANGELOG——沿 rc.22 同套 PR 序列）
2. tag `v0.2.0-beta.6-rc.23` → GHCR 正式镜像+SBOM+trivy 扫描+离线 tar+SHA256SUMS
3. GitHub prerelease（附件匿名逐位校验）+官网 releases 原子切换+README 对齐

## D. 生产切换（命令级）

```bash
# 1) 活体锚点（滤烙印：切换时显式 MERGEPILOT_VERSION，不从旧容器继承）
docker inspect beta-mp-console > /backup/rc22-anchor-$(date +%Y%m%d-%H%M).json
# 2) 数据库备份+哈希+隔离恢复核验
docker exec beta-mp-pg pg_dump -U <user> -Fc mu > /backup/mu-pre-rc23sw.dump
sha256sum /backup/mu-pre-rc23sw.dump | tee /backup/mu-pre-rc23sw.dump.sha256
docker run --rm -d --name mu-restore-check -e POSTGRES_PASSWORD=x postgres:16-alpine
# 空库恢复 + 行数抽查（tenant/repository_binding/fix_approval/review_run/job）
# 3) 迁移重演：恢复库上跑 rc.23 镜像（initSchema 增量 v24/v25）→ schema=25 + 旧数据断言
#    （不把准备轮演练代替目标库备份——本步在备份恢复库上执行）
# 4) 切换（仅 Console 容器）
docker stop beta-mp-console && docker rm beta-mp-console
docker run -d --name beta-mp-console ... <rc.23 digest> -e MERGEPILOT_VERSION=rc.23 ...
# 5) Gitee 受保护配置：生产 env 仅加授权试点仓库的连接（不自动绑定 PAT 可见全部仓库）
```

## E. 上线验收与 T+1h

版本/digest 核验（防 env 烙印：容器内实际版本=rc.23）→ schema=25 → 健康/401 门禁/代理/worker 绑定/Matrix/GitHub 链路 → 授权试点仓库的生产 Gitee 手动+webhook 核验（job/run/head/审计/结果）→ 桌面+390px 连接面板/平台链接/权限提示/范围声明 → 从实际切换时刻观察 1 小时（健康/重启/异常日志/代理/worker/Gitee 消费），**不创建 24h 任务**。

## F. 回滚限制（准确口径）

- rc.22 旧镜像对 Gitee 任务=明确拒绝（`rejected|installation_id_missing`，实测）——**不称 Gitee 业务无损回滚**；回滚前停止 Gitee 入队（撤销连接即可，存量任务已 rejected 可在恢复后重触发）。
- 不默认恢复旧 dump/丢弃上线后数据——数据库恢复=显式决策（数据取舍：连接行可弃/run 历史保留则不 DROP）。

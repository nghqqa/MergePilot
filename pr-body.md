## 范围（仅运维工具输出安全化，零语义变更）

rc.18 生产切换实录（2026-10-08）：swap-console.cjs 的 `log('+ docker run -d ' + args.join(' '))` 将 inspect 继承的 40 个 env（含 session 密钥/GitHub App 私钥/LLM key/PG 连接串/webhook secret/OAuth secret/AgentTeams token）**全量回显到运维转录**。本 PR 修复工具输出，不改变任何切换语义。

### 修复
- 新增纯函数 `safeSwapSummary`（白名单输出）：容器名/镜像/restart policy/网络/端口/挂载/env **键名与数量**——env 值与完整 docker run 参数在任何路径都不回显；不依赖敏感关键词黑名单
- dry-run 与正式执行统一走安全摘要；成功输出改为 `container=<id>`
- 失败路径仅透传 docker 自身 stderr（daemon 侧消息，不含调用参数）——原有行为保持并加注释成红线
- 语义不变：镜像切换/挂载/网络/端口/重启策略 floor（unless-stopped）/-e 全量继承与 extraEnv 不覆盖继承键——原样；MERGEPILOT_VERSION 烙印仍按既有做法在调用侧以 inspect 副本过滤（本 PR 不引入 env 过滤行为变更，既有契约未动）

### 测试（新增 4 例泄露回归，虚构七类凭据夹具）
session 密钥/GitHub App 私钥/LLM key/PG 连接串/webhook secret/OAuth secret/AgentTeams token+Matrix 密码：
1. dry-run：`[DRY-RUN]` 安全摘要、env=9 项仅键名、零秘密值、零 `-e 键=值` 拼接、不调用 docker
2. 正式执行成功：docker run 仍收到全部 9 个 -e（**继承语义不变**），输出仅 `container=<id>` + 安全摘要
3. 失败路径：退出码透传、daemon 错误消息透传、零秘密值零参数拼接
4. safeSwapSummary 纯函数：键名在、值不在

全量 **23/23 绿**（原 19 例含 rc.11 综合回归全保持）；secret-scan PASS。真实数据终验：修复后脚本对真实 inspect 档案 dry-run，除两处与白名单挂载点同字符串的**非凭据容器路径**（/app/keystore、/app/rag-models）外，秘密值泄露=0。

## 边界
不执行生产 swap；不轮换凭据；不改 RBAC/API；不删除既有转录（轮换与清理另行授权）。

# rc.35.5 发布材料与回滚方案（2026-10-15）

> 状态：候选就绪，生产切换保留既有最终确认节点（未切换）。
> 远端事实不可由容器回滚撤销：本版本新增的"应用修复/合并 PR"会把提交/合并写到
> 用户仓库——**容器回滚（swap 回旧镜像）不会撤销已经发生的远端提交或合并**；
> 远端回退只能在代码平台手动 revert。交付说明与验收沟通必须如实表达这一点。

## 1. 最终候选身份（切换前回填核验）

| 项 | 值 |
|---|---|
| 源码 | main（PR 归并后最终 commit，切换前以 `git rev-parse HEAD` 为准） |
| 版本 | v0.2.0-beta.6-rc.35.5（构建期 ARG/ENV 唯一真源） |
| 镜像 | ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.35.5 |
| 镜像 digest | 构建后回填（`docker images --digests`） |
| 前端 bundle | /api/health 的 frontend_bundle.file+sha256 与构建产物一致 |
| schema | v29（mu.fix_apply_operation；v28/v29 均纯新增，rc.35.4 代码前向兼容已实证） |

## 2. 变更清单（相对生产 rc.35.4）

- 应用修复闭环（#455~#458+收口波）：预览—确认绑定（expected_remote_head/expected_patch_digest
  缺一不可）、数据库原子认领（并发败方零覆写）、两阶段写（候选 SHA 写前持久化）、
  result_uncertain+读回对账端点、重审自动入队（webhook 幂等去重）、统一合并资格门
  （head 域审查证据+CI 双面判定+mergeable 未知不放行）、Gitee 推送式提交定案。
- 模型审查解耦与补丁验证链（此前 rc.35.x 系列已交付，生产 rc.35.4 已含）。
- B 线固定评测流水线（六组缺陷/正确配对样本，字节钉死入库）与 C 线交付文档。
- schema v29（纯新增表；无破坏性迁移）。

## 3. 发布步骤（生产切换确认后执行）

1. `git tag v0.2.0-beta.6-rc.35.5 <最终 commit>` 并推送 tag。
2. 推送镜像到 GHCR（`docker push ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.35.5`）。
3. Release（prerelease）+ 附件（SBOM/trivy/离线 tar/SHA256SUMS，按既有 rc 流程）。
4. swap 生产容器（`swap-console.cjs <inspect.json> <镜像> --expect-commit <commit>`），
   T+1h 观察（health/重启/异常/代理/官网一致性）。

## 4. 回滚方案

| 场景 | 动作 | 说明 |
|---|---|---|
| 容器/应用缺陷 | swap 回 rc.35.4 镜像（digest 2c03c04d…，源码 d80e7e6） | inspect 继承 env/挂载/网络；**滤版本烙印**；回滚镜像与 v29 库前向兼容（已实证 health ok） |
| schema 疑虑 | 不回滚迁移（v29 纯新增表，旧代码忽略） | 禁止手工 DROP；如需彻底移除属新迁移决策 |
| 远端已发生的提交/合并 | **容器回滚无效**——平台手动 revert | 应用与合并均为快进/条件写，revert 提交即可恢复内容 |
| Matrix/执行器 | ctrl 容器 `docker start`（restart 存活 cli-token） | 2026-10-15 实录：ctrl exit 255（原因未查明——不能写已根治），重启后 Matrix 200 |

## 5. 已知能力边界（如实交付）

- Gitee：**当前连接器的 CI 状态读取不支持**（实测两个 status 端点均 404）→ 合并资格门
  CI 部分恒 unsupported 阻塞。连接器能力结论，不断言 Gitee 平台整体不存在相关 API。
- GitHub 写侧需 App **Contents: Read and write**；CI 双面判定需 **Commit statuses: read**
  与 **Checks: read**（缺任一即 unsupported 并报告所需权限，不用 PAT 绕过）。
- 必需检查清单（branch protection required checks）需 admin 权限读取——本服务不预判，
  保护规则由平台在合并时强制（delegated_to_platform）。

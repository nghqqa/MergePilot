# 发布检查清单（制度化）

> 适用：每个官方预发布/正式版本。来源：rc.35.6-rc1（2026-10-17）构建未注入 GIT_COMMIT 的
> 流程遗漏复盘 + rc.35.6-rc2（2026-10-18）发布实践。**逐项执行、逐项留痕；未完成不发布。**

## 1. 冻结与命名

- [ ] 冻结源码 commit（main HEAD 或指定 commit），确认其后无未纳入修正；
- [ ] 版本号不与既有 tag/Release 冲突（`git ls-remote --tags origin` 完整列表比对；
      annotated tag 解引用行会绕过 `$` 结尾 grep——必须完整列表比对）；
- [ ] 保持候选身份命名（`-rcN` 等），不改称最终版本。

## 2. 构建（身份强制）

- [ ] **从干净检出构建**（`git worktree add <dir> <commit>`，`git status` 干净）；
- [ ] **必须注入 `--build-arg MERGEPILOT_VERSION=<准确版本>` 与
      `--build-arg GIT_COMMIT="$(git rev-parse HEAD)"`**（完整 40 位；漏注入=health
      `git_commit=null`，制品无法与源码绑定——rc.35.6-rc1 教训）；
- [ ] 基础镜像 digest 钉住；如需安全升级在 Dockerfile 显式声明（如 #483 的 zlib）。

## 3. 验证（合入后、发布前）

- [ ] 改动相关测试全绿（CI 必需检查全绿；新增测试必须注册进 CI 并核实执行）；
- [ ] 隔离栈启动：`/api/health` 的 `version` 与 `git_commit` **逐字符核对**与构建参数一致；
- [ ] 前端 bundle 指纹记录（health `frontend_bundle`）；
- [ ] zlib/依赖核验用权威命令：`apk version <pkg>`（installed : available）、
      `/lib/apk/db/installed` 条目、`apk info -L <pkg>`（文件清单）；
      **`apk info <pkg>` 的描述头不反映安装版本，不作依据**；
- [ ] trivy 扫描（vuln+secret）：与上一版对比，目标漏洞消除且无新增。

## 4. 发布

- [ ] 推送 GHCR（tag 与/或 digest），核对身份链：tag → OCI index → amd64 manifest → config
      四面取自 registry 实测（**不得手抄截断值或臆造尾段**）；
- [ ] 离线 tar（`docker save`+gzip）内容身份核对（tar 内 index/manifest/config 与 GHCR 一致）；
- [ ] SBOM + 扫描 JSON + SHA256SUMS 三件随 Release 上传；
- [ ] Release 标注 prerelease（候选版）；发行说明按 git 差实核实，含 schema/兼容/回滚边界
      与待验项（不宣称未执行的验收）；
- [ ] **匿名下载 SHA256SUMS 所列全部文件逐位校验**（大文件可经代理续传，仍属匿名路径）。

## 5. 生产换版

- [ ] 换版前保全：旧容器 inspect（本机非公开目录）、DB pg_dump、旧镜像引用（回滚锚）；
- [ ] `scripts/swap-console.cjs <inspect.json> <digest 引用>`（**用 digest pin**；脚本已过滤
      版本烙印 env——新镜像烙印即 health 真源）；先 `--dry-run` 看摘要；
- [ ] **inspect 输入必须是原始 `docker inspect` 输出**（脚本会自动拒绝掩码/缺项/陈旧输入）；
      脱敏副本仅用于展示/归档，不得作为部署输入；
- [ ] 换版后只读核查：version/git_commit/bundle/schema/成员角色/错误日志；
- [ ] **T+1h 观察满窗并记录采样后才可宣称通过**；触发回滚条件即切回旧 digest 并留证据。

### swap 工具安全门（rc3 事故回归锁）

工具在停止旧容器前依次执行以下校验，任一失败即安全中止（旧容器不受影响）：

| 门 | 校验内容 | 退出码 |
|---|---|---|
| 部署完整性 | 掩码标记（`***`/REDACTED）与 multiuser 必填项 | 4 |
| Matrix 凭据 | live 容器内实际登录（identifier 格式） | 3 |
| live 比对 | 部署输入 vs 运行容器的镜像身份 + env 键集 | 5 |
| 回滚可用性 | 回滚镜像本地可解析且身份与切换前一致 | 6 |

回滚路径：不可变镜像 ID 优先；回滚后校验镜像身份+健康+DB 就绪。
Config.Image 兜底仅在解析身份与原镜像一致时允许（漂移 tag 拒绝）。

- [ ] 换版前保全：旧容器 inspect（本机非公开目录）、DB pg_dump、旧镜像引用（回滚锚）；
- [ ] `scripts/swap-console.cjs <inspect.json> <digest 引用>`（**用 digest pin**；脚本已过滤
      版本烙印 env——新镜像烙印即 health 真源）；先 `--dry-run` 看摘要；
- [ ] 换版后只读核查：version/git_commit/bundle/schema/成员角色/错误日志；
- [ ] **T+1h 观察满窗并记录采样后才可宣称通过**；触发回滚条件即切回旧 digest 并留证据。

## 6. 收尾

- [ ] 官网（website/）版本/身份/扫描经文档 PR + 检查更新后部署，公开 HTTPS 验证实返内容；
- [ ] 关联 issue（如安全修复）在证据齐全后关闭。

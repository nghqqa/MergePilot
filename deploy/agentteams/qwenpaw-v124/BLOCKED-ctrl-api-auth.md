# BLOCKED：v1.2.4 embedded ctrl 管理 API 认证（2026-10-24，干净环境首装波）

## 状态

四角色真实任务验收 **BLOCKED** 于第一步「四 worker 置备」：ctrl 管理 API（:8090 /api/v1/*）
对**所有已试 bearer 家族**返回 401 `invalid or missing bearer token`——包括 ctrl 自签发的
现行 cli-token。

## 已试家族与结果（全部 401）

| # | 凭据 | 结果 |
|---|---|---|
| 1 | ctrl 自签 cli-token（`/var/run/agentteams/cli-token`，读后即用） | 401（20:44–21:10 连续 100/100 采样全 401） |
| 2 | `/data/pki/token.csv` 静态 token（apiserver `--token-auth-file`，user=admin/system:masters） | 401 |
| 3 | 自铸 ES256 SA JWT（ctrl 自身 `/data/pki/sa.key` 签发；kid=RFC7638/SPKI 变体×iss 有无 共 8 组合） | 全 401 |
| 4 | `AGENTTEAMS_AUTH_TOKEN` env 直钉稳定值（容器重建注入） | 401（env 存在但不生效） |
| 5 | `AGENTTEAMS_AUTH_TOKEN_FILE` 指向卷内受控文件（值=稳定 token，boot 期读入） | 401 |
| 6 | 裸 token（无 Bearer 前缀）/ 宿主侧 / 容器内 双路径 | 均 401 |

## 关键证据

- **同 digest 曾通过**：2026-10-11 qwp2 评估（Windows Docker Desktop 引擎）对同一
  `agentteams-embedded:v1.2.4`（sha256:d96eb4dd…）cli-token 认证的 workers API 全 PASS
  （REPORT.md §3.1 P1-2=200；deploy-candidate-qwp2.sh 同认证形状）。今日 virgin first boot
  （全新卷/全新凭据/单次 boot）复现 401——**环境性差异未定位**。
- **TokenReview 缺失**：controller 二进制含 `backend.K8sTokenReviewClient` + "token review
  request failed"；kubeconfig 指向 `https://localhost:18443`——该端口的
  `/apis/authentication.k8s.io/v1[/v1beta1]/tokenreviews` = **404**（POST 落 discovery 列表）。
  真 kube-apiserver 监听 **6443**（`--token-auth-file=/data/pki/token.csv
  --service-account-signing-key-file=/data/pki/sa.key --service-account-issuer=https://agentteams.local
  --authorization-mode=AlwaysAllow --anonymous-auth=true`），6443 上 TokenReview 请求自身 401。
- **gateway/Matrix/MinIO 与认证无关**：Higress 真实 LLM 往返 200（api.deepseek.com 上游，
  测试 key）、Matrix 200、MinIO 200——仅管理 API 认证面锁死。
- 两次孤立 200 窗口（重启后数分钟）不可复现、无规律。

## 影响与后续解锁路径（供下一轮/上游）

1. 向上游（AgentTeams v1.2.4 = 8765d6973b80…）求证 18443 TokenReview 404 是否为该
   embedded 构建缺陷，及 qwp2 评估期认证可用的环境前提；
2. 或复刻 qwp2 评估期卷状态（qwp2-data 原卷已清，无法直接对照）；
3. 或以生产 ctrl（agentteams-beta-controller，数据卷含 RSA pki 旧线凭据）对照其认证面行为
   （生产引擎本轮处于暂停指令下未启动）。

## 本波已交付（不受阻塞影响）

统一安装入口 `install-agentteams-full.sh`（依赖 digest 核验→apply→四 worker 置备→CR env
校验→console 接线清单→verify——worker 步起被本阻塞卡住）、`mint-sa-token.sh`（RSA 线可用）、
apply 网关探测重试（envoy 同步滞后修复）、旧线脚本 223ddc2 守卫、三镜像 digest 核验、
候选离线包 load 身份核对、embedded 内存实测 **1.68GiB 常驻**（idle，含 Higress×5+envoy+
Tuwunel+MinIO+ctrl）。

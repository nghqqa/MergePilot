# tuwunel 镜像来源追溯（7.3F）

**结论：`CONFIGURATION_READY_FOR_RESTORE`** —— 来源可验证且镜像本体就在本地，无需拉取、无臆造成分。

## 命中链
1. `docker images` 名称过滤为阴性（20 tagged + 5 dangling 无一含 matrix/tuwunel 字样）；
2. 转向 imagedb **内容 blob** 字符串检索：`/var/lib/docker/image/overlay2/imagedb/content/sha256/44a29e0d…`
   命中 `tuwunel /usr/local/bin/tuwunel # buildkit`；
3. `docker image inspect 44a29e0d` → blob 归属
   **`higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-embedded:v1.1.2`**
   digest `sha256:5f8b42fd6c4160b40eb7c3b26c5617edc78fe24d2fcb00f918ff6d742aaa2d2c`（2026-05-27 构建）；
4. 直接读取镜像内 `/opt/hiclaw/scripts/init/start-tuwunel.sh` 拿回完整启动配方：
   conduwuit fork、`CONDUWUIT_*` 环境族、监听 `0.0.0.0:6167`、DB `/data/tuwunel`、
   **`CONDUWUIT_ALLOW_REGISTRATION=true`**、`HICLAW_MATRIX_DOMAIN` 默认
   `matrix-local.hiclaw.io:8080`（历史实际值 `:18080`，见 matrix-users.json server_name）。

## 历史形态解释
Matrix 服务端从来不是独立镜像——它内嵌在 hiclaw-embedded 容器中随其生命周期存在；
容器清理时"Matrix 消失"，但镜像标签一直在本地，被名称过滤漏检。

## 恢复待授权项
① 载体确认（本地镜像，零拉取）；② m8gh4-* 五用户/房间/令牌重建授权（注册是镜像内建支持流程）；
③ 网段决策（R4 冻结 172.22.0.2 或新 pinned IP，同步刷 github-e2e.json/room-map）。

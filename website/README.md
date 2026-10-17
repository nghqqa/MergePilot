# 官网文档维护

`website/` 是 `docs.nghqqa.cn` 的静态源码，不是托管 Console。修改和验证源码不会部署官网。

## 内容来源与版本

- `user-guide.html`：面向使用者的入口，详细流程与排障分别来自
  [`USER-GUIDE.md`](../docs/delivery/USER-GUIDE.md) 和
  [`TROUBLESHOOTING.md`](../docs/delivery/TROUBLESHOOTING.md)。
- `quickstart.html`、`selfhost-docker.html`：以
  [`deploy/selfhost/README.md`](../deploy/selfhost/README.md)、实际 compose 和 Dockerfile 为准。
- `downloads.html`：已发布制品以对应 GitHub Release 的 tag、附件、扫描与校验和为准。
  main 或试用候选中的修正放在「发布与源码范围」，不能悄然替换官方 tag/digest。
- `limitations.html`：记录能力与验证边界；代理走查、维护者自测和独立新用户验证分别陈述。
- 导航的唯一来源为 `nav.mjs`。新增文档页时，同时更新其清单、`docs.html` 正文入口，
  并运行导航生成器；不要逐页手改生成区域。

本轮核对基线为 main `4c61ef1`（2026-10-17）；官方 rc.35.5 制品仍对应 `af8b0022`。
不改历史证据或 Release，下一次正式发布时再核对并更新制品范围。

## 本地验证

从仓库根目录执行（Node.js ≥ 20，无需安装依赖）：

```bash
node website/nav.mjs           # 导航清单有改动时生成
node website/nav.mjs --check
node website/check.mjs
node website/asset-hash.mjs --check
node website/behavior-test.mjs
```

只有修改 `assets/style.css` 或 `assets/nav.js` 时才运行
`node website/asset-hash.mjs` 生成内容哈希文件。纯文案修改无需重建 Console 或试用镜像。

官网部署由发布流程单独执行；不要借文档更新切生产、重建正在试用的候选或重置试用数据。

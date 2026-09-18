# PR 任务元数据（run-elem-fastapi-pr3-20260916-01）

- 仓库：https://github.com/nghqqa/fastapi-boilerplate-demo （公开仓）
- PR：#3 https://github.com/nghqqa/fastapi-boilerplate-demo/pull/3 （OPEN）
- 标题：High-risk security demo: human REJECT path (no remediation authorized)
- head 分支：demo/high-risk-human-reject
- head SHA：ad267a6e51209551a0733657321bb364d04befd0（所有角色必须在此 SHA 上工作）
- base SHA：4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c（base 分支 mergepilot-demo/schema-migration-risk）
- 变更：+82/-0，新增 2 文件（含一个演示用命令执行端点及其测试）

## 统一 clone/checkout 命令（所有角色相同，在自己工作区执行）
cd ~ && git clone --quiet https://github.com/nghqqa/fastapi-boilerplate-demo.git pr3-work
cd ~/pr3-work && git checkout --quiet ad267a6e51209551a0733657321bb364d04befd0
git rev-parse HEAD   # 必须输出 ad267a6e51209551a0733657321bb364d04befd0，否则停止并报告

## 依赖（已预装）
使用 /opt/venv/standard/bin/python（fastapi/httpx/pytest 已预装）；不要用裸 `python`。

## 冻结约束（全角色）
- 禁止修改仓库任何现有测试文件
- 禁止 push / 建 PR / 评论 / merge（本轮零 GitHub 写入）
- 交付与回帖只发生在 Matrix 房间与本地工作区

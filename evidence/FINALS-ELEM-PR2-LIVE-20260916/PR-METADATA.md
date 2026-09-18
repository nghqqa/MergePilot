# PR 任务元数据（run-elem-fastapi-pr2-20260916-01）

- 仓库：https://github.com/nghqqa/fastapi-boilerplate-demo （公开仓）
- PR：#2 https://github.com/nghqqa/fastapi-boilerplate-demo/pull/2 （OPEN）
- 标题：High-risk security demo: require human verification before remediation
- head 分支：demo/high-risk-human-gate
- head SHA：1dedf5e1992c950557064d8f4fb9039d1523deb3（所有角色必须在此 SHA 上工作）
- base SHA：4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c（base 分支 mergepilot-demo/schema-migration-risk）
- 变更：+122/-0，新增 2 文件：
  1. backend/src/interfaces/api/v1/demo_high_risk.py（+42）
  2. backend/tests/unit/test_demo_high_risk_path_traversal.py（+80）

## 统一 clone/checkout 命令（所有角色相同，在自己工作区执行）
cd ~ && git clone --quiet https://github.com/nghqqa/fastapi-boilerplate-demo.git pr2-work
cd ~/pr2-work && git checkout --quiet 1dedf5e1992c950557064d8f4fb9039d1523deb3
git rev-parse HEAD   # 必须输出 1dedf5e1992c950557064d8f4fb9039d1523deb3，否则停止并报告

## 依赖（如缺失则安装）
python -m pip install --quiet fastapi httpx pytest

## 冻结约束（全角色）
- 禁止修改 backend/tests/unit/test_demo_high_risk_path_traversal.py 及仓库任何现有测试
- 禁止 push / 建 PR / 评论 / merge（本轮零 GitHub 写入）
- 交付与回帖只发生在 Matrix 房间与本地工作区

# CASE-MANIFEST · run-elem-pr2r2-20260916-01（仅参数；行为契约见 ~/task/ROLE-CONTRACT.md）

## 标识
- run_id: run-elem-pr2r2-20260916-01
- project_id: elemiso-pr2r2-gate（team elemiso-team）
- repo: nghqqa/fastapi-boilerplate-demo（公开仓，只读）
- PR: #2（状态 OPEN；零 GitHub 写入）
- head 分支 / head SHA: demo/high-risk-human-gate / 1dedf5e1992c950557064d8f4fb9039d1523deb3
- base SHA: 4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c（分支 mergepilot-demo/schema-migration-risk）
- 变更规模: 2 files +122/-0

## 统一 clone/checkout（各角色用自己的目录名）
cd ~ && git clone --quiet https://github.com/nghqqa/fastapi-boilerplate-demo.git <你的工作目录>
cd <你的工作目录> && git checkout --quiet 1dedf5e1992c950557064d8f4fb9039d1523deb3
git rev-parse HEAD   # 必须输出上述 SHA，否则停止并报告 BLOCKED

## 任务
- pr2r2-review-1 → pr2r2-fix-1 → pr2r2-verify-1（DAG 已就绪，勿增删任务）
- fix 最大重派轮数: 2

## 环境
- python: /opt/venv/standard/bin/python（fastapi/httpx/pytest 已预装；勿用裸 `python`）
- 禁止: rm -rf、修改仓库任何文件（含测试）、push/PR/评论/merge、读取其他角色工作区

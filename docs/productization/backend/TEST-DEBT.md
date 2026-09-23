# TEST-DEBT — 全树测试口径（2026-09-23 建立）

口径：**本轮门禁 = 相关目录绿**（tests/gh_bridge + tests/model_gateway 等改动面）；
全树不再默认按"既有失败"绿灯放行——本文件登记全部已知非绿项，逐项收口。

## 已修复（2026-09-23）

- **收集冲突（原 3 errors，曾致全树无法单进程运行）**：tests/m4b、m4c、m4e、skills
  存在同名测试模块（test_integration.py / test_contract.py）且目录无 `__init__.py`。
  修复 = `pyproject.toml` 设 `addopts = --import-mode=importlib`（按路径导入，
  消除 basename 冲突；不添加 `__init__.py`，避免改变这些套件依赖的 sys.path 自举）。
  修复后全树收集 4345 项 / 0 错误。**曾尝试给 16 个目录补 `__init__.py`，会破坏
  m4b/hiclab/skills 等依赖 conftest sys.path 自举的套件（skills.common 解析），
  已回退——不要重试该方案。**

## 待收口清单（84 failed + 21 setup errors，2026-09-23 全树实测）

明细：`r3work/model-switch/fulltree-failures-20260923.txt`、
`fulltree-errors-detail-20260923.txt`。按模块归因：

| 模块 | 数量 | 根因类别 | 收口方向 |
|---|---|---|---|
| tests/m5_0c/test_image_resolver.py | 38 | 依赖宿主 docker CLI/本地镜像（resolver 返回空） | 测试内 fake docker 已有；补齐 CLI 探测跳过守卫或固定 fixture 镜像 |
| tests/demo_console/test_showcase_materials.py | 16 | 工作树缺烘焙产物（如 Dockerfile.demo-console 等） | 产物生成脚本入 repo 或测试前置构建 |
| tests/isolated_live/* | 22 | 需 live compose 环境（设计如此） | 移入标记套件（`-m isolated`），CI 定期跑，本地门禁跳过 |
| tests/m4c/test_test_runner.py | 3 | runner 断言依赖打包环境 | fixture 化 test-runner 调用 |
| tests/m4e/test_contract.py | 1 | 证据复算依赖完整证据目录 | 与 m4f1 证据线一并修 |
| tests/m4f1/test_release_evidence.py | 1 | bash wiring 检查路径依赖 | 路径参数化 |
| tests/demo_console/test_dynamic_refresh.py | 1 | 同 showcase（Dockerfile 缺失） | 同上 |
| tests/isolated_live/test_demo_console_entrypoint.py | 21 setup errors | 全部集中于该单模块（需 compose 环境） | 随 isolated_live 标记方案一并处理 |

原则：不修断言、不降标准；只补环境守卫（skip with reason）/缺失产物/隔离标记。
逐项收口后在本表打勾并更新全树基线数。

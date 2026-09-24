# CASE2-B 人工补丁验证报告（2026-09-24T02:54Z，状态校准轮）

验证人角色：**人工复核（human-directed verification，AI 执行、零模型调用）**。
本报告不是生产 Verifier（iso_chain/verifier.py）执行记录——生产 Fixer/Verifier 全程零启动。

## 1. 被验证补丁

| 项 | 值 |
|---|---|
| 补丁文件 | `evidence/rpd-24h/case2b-fix/patch.diff`（本轮只读使用，未修改） |
| SHA256 | `6d9e9905ed4ccb1c8c1126a220425268e92e1414219b767d0f398f9602070090` |
| 适用目标 commit | `42ed17879becbc02e31551938afbbf689351df96`（nghqqa/fastapi-boilerplate-demo，PR #2 head） |
| 修改文件 | `backend/src/interfaces/api/v1/demo_high_risk.py`（修复）、`backend/tests/unit/test_demo_high_risk_containment.py`（新增 123 行）、`backend/tests/unit/test_demo_high_risk_path_traversal.py`（PoC 断言反转） |
| diffstat | 3 files changed, 167 insertions(+), 30 deletions(-) |
| 目标 blob 匹配 | patch index 头 `59cd8d1`(demo_high_risk.py) / `a1db63a`(path_traversal) 与 `git ls-tree 42ed1787` 实测完全一致 |

PR #2 远端状态核验（2026-09-24T02:54Z）：OPEN，headRefOid=42ed1787…（=补丁目标 commit，无漂移）。

## 2. 验证环境

- 一次性临时 clone：`$(mktemp -d)/repo`（系统临时目录，验证后销毁，见 §7；**未使用生产/共享任何资源**）
- Windows 10 / Git Bash；Python 3.9.25（miniconda）；pytest 8.3.5、fastapi 0.128.8、httpx 0.28.1、anyio 4.9.0
- 模型请求数（本轮验证全程）：**0**；模型 token：**0**

## 3. 干净应用验证

```
$ git apply --check --verbose evidence/rpd-24h/case2b-fix/patch.diff
Checking patch backend/src/interfaces/api/v1/demo_high_risk.py...
Checking patch backend/tests/unit/test_demo_high_risk_containment.py...
Checking patch backend/tests/unit/test_demo_high_risk_path_traversal.py...
exit=0

$ git apply evidence/rpd-24h/case2b-fix/patch.diff   → exit=0
 M backend/src/interfaces/api/v1/demo_high_risk.py
 M backend/tests/unit/test_demo_high_risk_path_traversal.py
?? backend/tests/unit/test_demo_high_risk_containment.py
```

**结论：补丁可干净应用 ✓**

## 4. 修复后测试（补丁携带用例）

命令（在临时 clone 的 `backend/` 下）：

```
python -X utf8 -m pytest tests/unit/test_demo_high_risk_containment.py \
  tests/unit/test_demo_high_risk_path_traversal.py -v -p no:cacheprovider \
  --noconftest -o addopts=""
```

运行参数说明（如实记录）：目标仓库 `tests/conftest.py` 依赖 `pytest_asyncio`（本验证环境未安装），
pyproject `addopts` 启用 `--strict-markers` 而其 `security_demo` marker 亦依赖 conftest 注册；
被测两文件**自包含 fixture（文件内定义 `env` fixture、importlib 直载模块）**，不依赖仓库 conftest，
故以 `--noconftest -o addopts=""` 隔离运行。此为环境隔离选择，不改变被测代码与断言。

结果：**13 passed, 1 skipped, exit=0**（0.84s）

| 组 | 用例 | 结果 |
|---|---|---|
| 逃逸拒绝 | `../outside-secret.txt`、`../../`、`../../../etc/hostname`、绝对路径 `/etc/hostname`、反斜杠 `..\..\`、编码变体 `..%2F`、`sub/../../`（共 7） | 全部 PASSED（400/404，无 TOP-SECRET/HOSTNAME-LEAK 泄漏） |
| 逃逸拒绝 | symlink 逃逸 | **SKIPPED**（Windows 创建 symlink 需特权，测试内建 `pytest.skip`；realpath 逻辑对该向量的防护未被本机覆盖，如实登记） |
| 逃逸拒绝 | 空名 → 404 非 base 列表 | PASSED |
| 合法路径回归 | `inside.txt` 200 含 LEGIT-INSIDE；`sub/subfile.txt` 200 含 SUB-OK | PASSED |
| 合法路径回归 | base 内不存在文件 → 404 | PASSED |
| PoC 反转 | `test_high_risk_path_traversal_now_contained`（原 PoC：期望 400 且无 TOP-SECRET-OUTSIDE-BASE） | PASSED |
| PoC 反转 | `test_high_risk_depth_now_contained`（深层逃逸：期望 400 且无 DEEP-SECRET） | PASSED |

## 5. 对照验证（补丁前，证明"原始 PoC 被阻断"的前后对照）

同一临时 clone、`git stash` 暂存补丁后运行目标仓库 42ed1787 原始（未修复）PoC 文件：

```
python -X utf8 -m pytest tests/unit/test_demo_high_risk_path_traversal.py -v \
  -p no:cacheprovider --noconftest -o addopts=""
→ 1 passed, 1 failed, exit=1
```

- `test_high_risk_path_traversal_reproduces`：**PASSED（未修复代码上复现泄漏：`../outside/outside-secret.txt` 返回 200 且含 TOP-SECRET-OUTSIDE-BASE）**——证明漏洞在 42ed1787 真实存在，且 §4 中同一用例反转后通过构成有效阻断对照。
- `test_high_risk_path_traversal_depth`：FAILED。**根因属原始测试自身缺陷，与补丁无关**：该用例把 `deep-secret.txt` 写在 `tmp_path`，而 `demo-files/sub` 起连升 3 级（`../../../`）解析到 `tmp_path` 的父目录，目标文件实际不存在；Windows 下 FileResponse 触发 `FileNotFoundError → 500`，断言 200 失败。（该缺陷在补丁前的目标仓库即存在；补丁未修目标仓库任何文件，只将本用例断言反转为 containment 语义并覆盖通过。）如实登记，不作为补丁失败。

## 6. MANUAL_FIX_VERIFIED 四条件核对

| 条件 | 结果 | 证据 |
|---|---|---|
| 补丁可干净应用 | ✓ | §3 `git apply --check` exit=0 |
| 原始 PoC 被阻断 | ✓ | §5 未修复复现泄漏（主用例）vs §4 修复后同向量 400 无泄漏 |
| 合法路径回归通过 | ✓ | §4 TestLegitimateReads 3/3 PASSED |
| 相关测试通过且证据摘要完成 | ✓ | §4 13 passed/1 skipped exit=0 + 本报告 |

## 7. 边界与清理声明

- **模型请求新增 0、token 新增 0**（人工验证路线，未调用任何真实/付费模型；CL-08 模型路线本轮零动作）。
- 生产 Fixer/Verifier（AgentTeams 角色、iso_chain 派发）**零启动**；本验证为本地一次性副本上的人工复核。
- 未向目标仓库 push、未创建/修改 PR #2、未发布 check-run、未写任何共享环境。
- 验证用临时 clone 于报告落盘后删除（`rm -rf` 一次性目录）。
- 补丁本身（patch.diff）本轮未修改；其生成过程属 CASE2-B 人工修复流（另行记录）。
- **本验证 ≠ TicketStore 正式审批闭环，≠ D-B 批准，≠ 生产 Verifier 执行**；补丁的正式派发/合并仍待 D-B + 合法票据 + 用户明确指示。

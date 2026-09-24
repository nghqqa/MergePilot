# CASE2-B HIGH finding 人工修复报告（FIX_READY → VERIFIED）

**finding**: CWE-22 任意文件读取（HIGH），`demo_download` 用户可控 `name`
直 join 基目录无包含校验。**处置**：操作员批复 B（批准进入 fix 计划），
人工实现修复（CL-08 选项 c——不经模型产出补丁）。

## 修复内容（分支 `fix/cwe22-containment` @ 业务仓库本地 clone）
- commit `f95d99d`（amend 含边界测试迭代，父=`42ed178`）
- `demo_high_risk.py`：`os.path.realpath(os.path.join(base, name))` +
  `os.path.commonpath` 包含校验（组织标准 file-path-containment 模式）；
  逃逸→HTTP 400（不回显服务器路径）；缺失→404；服务文件名取 basename；
  模块/路由描述更新为"已修复"。
- 旧测试反转：原"复现泄漏"断言改为包含断言（400/不泄露）。
- 新增边界测试 `test_demo_high_risk_containment.py`：7 种逃逸变体
  （../、绝对路径、混合分隔符、URL 编码、深嵌套、symlink 逃逸）+
  合法读取（根/子目录/缺失）+ 空名。

## FIX_READY
- 补丁：`patch.diff` 10539B，**sha256 = 6d9e9905ed4ccb1c8c1126a220425268e92e1414219b767d0f398f9602070090**
- 干净 checkout（42ed178 fresh clone）`git apply --check`：**PASS**

## VERIFIED（独立复跑，独立 checkout + 受限容器）
- 环境：独立 clone（非修复工作区）、mp-iso-test 容器、network=none、
  非 root(1000:1000)、512m/1cpu、pids 128、超时 120s、零秘密挂载
- 原始 PoC 复跑（poc-rerun.py，before/after 对照见 poc-results.json）：
  - `../outside-secret.txt`：修复前 HTTP 200 泄露 → **修复后 400 invalid path** ✓
  - `../../../etc/hostname`：修复前 HTTP 200 泄露 → **修复后 400 invalid path** ✓
  - `inside.txt`（合法）：**200 LEGIT-INSIDE**（合法路径未回归）✓
- 单测：14 passed（containment 边界 10 + 反转回归 4）
- 完整可行回归：backend/tests/unit 相关三文件 14/14；仓库其余套件依赖
  完整服务栈（数据库/Redis/逐仓 conftest），不属"可行"范围，未跑——如实声明。

## 残余风险与限制（如实）
- 修复仅覆盖 demo 模块；同仓其他端点未在本 finding 范围。
- `/etc/hostname` 逃逸现返回 400（含校验先于存在性检查），符合标准
  （不区分 400/404 以免回显路径存在性）。
- 该修复基于业务仓库本地分支，**未推送业务仓库、未合并 PR #2**（需授权）。
- 模型 fixer 路线（CL-08）仍 BLOCKED（flash reasoning 耗尽上限）——与本
  人工修复互不影响。

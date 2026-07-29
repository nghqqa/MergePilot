# M4-A · 公共 Skill Contract / runtime / 测试脚手架 — 证据

本轮（M4-A）落地公共层：统一 Contract（Draft 2020-12）、公共 runtime、测试 harness。
不包含任何业务 Skill。生成日期：2026-07-29（第四轮加固后终版）。

## 目标

为后续 6 个核心 Skill 提供单一、可复用、可独立测试的契约与运行时基座。

## 文件范围（本轮新增/修改）

新增：
- `skills/common/__init__.py`、`requirements.txt`、`requirements-dev.txt`
- `skills/common/schema/{request,response}.envelope.schema.json`（Draft 2020-12；response 含 `allOf` if/then）
- `skills/common/runtime/{__init__,errors,redact,envelope,cli}.py`
- `tests/skills/{conftest,test_contract,scan_delivery,run_all}.{py,sh}`
- `evidence/m4/m4a/{README,test-output-r1,test-output-r2,verification}.{md,txt}`

修改：`THIRD_PARTY.md`（仅追加 M4-A 依赖小节）。

## 加固要点（经四轮复审）

**输出隔离（fd 级 + Python stream 级）**：Skill 执行**和模块导入**期间，`os.dup2` 把真实 fd 1/2 重定向到临时文件，并保存/恢复 `sys.stdout/sys.stderr` 对象——覆盖 Python `print`、`os.write(1,…)`、子进程继承、导入期输出、Skill 重赋值/关闭 `sys.stdout`。捕获的 stdout/stderr 经脱敏路由到真实 stderr，**真实 stdout 只输出 envelope**。
**稳定发射 fd**：模块加载即 `os.dup` 保存真实 stdout/stderr fd；`_emit` 经此稳定 fd 写 envelope，即使 Skill 关闭了 `sys.stdout`/fd 1 也能输出。
**进程不静默退出**：Skill 或其导入抛 `SystemExit`→`INTERNAL_ERROR` envelope（非空 stdout），不再直接终止进程；argparse 错误→`INVALID_INPUT` envelope；仅 `--help` 正常退出。
**关联 ID 安全**：请求先校验才采用 `request_id/trace_id`，解析在之后，失败用安全占位。
**序列化保证**：`_finalize` 显式 `json.dumps` 校验；非可序列化输出→`INTERNAL_ERROR`，不用 `repr()` 掩盖。
**元数据净化**：`_rebuild_internal` 对 `name/version/request_id/trace_id` 做安全回退。
**1 MiB 真不可绕过**：`enforce_limits` 四遍（字段截断→字符串减半→output 替换(原始 SHA)→数组截断→兜底清空），`deepcopy` 不改调用方。
**Deadline 前后 check**；**Schema if/then**；**timeout 校验**（CLI+程序级）；**started_at 起点**；**AI 大小写不敏感**；**扫描器命中即 exit1**。

## 依赖版本（隔离 venv 实测固定）

- runtime：`jsonschema==4.25.1`（MIT）；dev/test：`pytest==8.4.2`（MIT）。
- 许可证核对自 site-packages dist-info；**完整 `pip freeze` 记录在 `verification.txt`**。

## 测试矩阵（75 项，固定常量）

`EXPECTED_PASS=75`（单一来源 `conftest.py`）。
- 1–9 Schema、10–13 状态条件、14–20 退出码、21–24 CLI 行为、25–34 脱敏、35–38 输出限制、39–41 side_effects。
- 42–56 第一轮；57–65 第二轮；66–71 第三轮；**72–75 第四轮**：72 导入期 `SystemExit`→JSON、73 Skill 重赋值 `sys.stdout`→envelope 仍输出、74 Skill 关闭 `sys.stdout`→稳定 fd 仍输出、75 子进程继承流被隔离。

## 两轮稳定运行

`run_all.sh` 连续跑 pytest 两轮，分别落 `test-output-r1.txt` / `test-output-r2.txt`，二者均须 `passed==EXPECTED_PASS`、`failed==0`、`rc==0`。`verification.txt` 记录两轮计数与完整 freeze。

## 门禁顺序

`run_all.sh` 自扫描在 `ALL GATES PASSED` 摘要**之前**执行并计入 FAIL。

## 已知边界

- **超时为协作式 Deadline**（非子进程强杀）；真正硬隔离留给各 Skill 容器（如 M4-C TestRunner）。
- fd+stream 隔离覆盖 Python `print`/`os.write(fd)`/子进程继承/导入期输出/Skill 重赋值或关闭 `sys.stdout`；远端网络写由各 Skill 自管。
- 公共层只**校验** `side_effects` 声明结构；真实副作用由容器/Gateway/负向测试保证。
- `redact.py` 是脱敏器与凭据正则**单一来源**（`credential_patterns()`），`scan_delivery.py` 复用。
- **未改 `.gitignore`**：`__pycache__` 由 `run_all.sh` 启动自清 + `PYTHONDONTWRITEBYTECODE=1` 保证无残留（如希望长期忽略，待复审示下）。

## 未触碰的 M3 文件

未修改 `tools/policy-gateway/*`、`tools/workflow-controller/*`、`tools/audit-db/*`、`tools/approve.sh`、`skills/sast-scan/*`、`skills/gh-mcp/*`、`tools/rag/*`、`config/souls/*`、`docs/*`、任何 `evidence/m3*`。未创建任何业务 Skill 目录。仓库现有 **29 个**历史 tag，全部原位未动。

## 提交状态

本轮**未 commit / 未 tag / 未 push**，未改写历史，未移动任何旧标签。新文件为未跟踪状态。等待复审后再决定 delivery/docs commit 与 `m4a-runtime-closed` 标签。

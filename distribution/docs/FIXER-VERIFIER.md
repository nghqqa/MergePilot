# Fixer/Verifier 使用边界

## 状态
- 代码模块就绪（iso_chain），测试通过（29/29 + canary 20/20）
- **生产容器未启动**
- 仅在隔离 fixture/clone 中验证

## Fixer
- 输入：目标代码 + finding + 测试文件 + 组织标准
- 输出：统一 diff（realpath containment 修复）
- 预算守卫：经 budget 模块控制模型调用
- 产物绑定：run_id / head_sha / ticket_id / attempt / patch_sha256

## Verifier
- **独立性**（结构强制）：不接受 fixer_reasoning
- 输入：finding + patched_code + patch_diff + test_results（来自 harness）
- 判定：VERIFIED | REJECTED（仅二值）
- 测试失败 → 必定 REJECTED

## 联调（隔离 fixture）
- CAS fencing：APPROVED→EXECUTING 仅一次
- Head 新鲜度：stale head 拒绝
- Patch digest 漂移检测
- 重复运行 → 同一 ticket（幂等）
- 并发竞争 → 仅一个执行

## ⛔ 禁止事项
- **不能**自动处理真实 PR
- **不能**修改真实仓库
- **不能**替代人工审查
- **不能**在无 receipt 时产生 success
- **不能**绕过 head/patch/repo 绑定检查

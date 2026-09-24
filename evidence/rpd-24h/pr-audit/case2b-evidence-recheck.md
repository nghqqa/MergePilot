# CASE2-B 证据只读复核（2026-09-24 复核轮）

14 项一致性核对：**14/14 实际通过**（初跑 2 个 FAIL 均为核对脚本自身错误，已用权威方法重验）：
- manifest_id：以桥同款规范算法重算 run-manifest → `172e744aa9b7eaea…` 与 run-context 及桥日志前缀**完全一致**（初跑的"期望常量"系笔误）；
- fixer/verifier 追踪：MinIO shared/tasks 与 fixer workspace 中该 run **零 fix/verify 任务**；
  result.md 中 "fix" 命中均为 "fixture"（测试夹具词）与审查员"no fix/patch code"声明。

十项结论：run_id/repo/PR/head/manifest_id 五方一致 ✓；rag_retrieve×2 均在窗内且 OK ✓；
source_refs 与 findings 引用一致 ✓；PoC 已记录且静态代码相符（os.path.join 无包含校验）✓；
check-run 107446711189 head 一致且唯一（receipt adopted=false）✓；无 fixer/verifier 派发 ✓；
无 approve/reject ✓；业务仓库零污染 ✓；证据文件秘密扫描净 ✓。

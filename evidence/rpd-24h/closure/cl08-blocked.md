# CL-08 阻塞说明（2026-09-24）

## 已验证可工作的部分（真实组件）
- 结构化建票：`tkt-*` 多次幂等创建成功（证据绑定，refuse 路径亦验证）
- iso 审批：CAS 通过（显式身份 operator-iso-night）
- 派发：start_exec fencing + outbox SENT→EXECUTED 流转正常
- 网关桥：container_gateway 宿主→容器→网关链路通（**真实 usage 计量可靠**）
- 预算守卫：reserve/commit/gap/保守计账全部实测生效

## 阻塞根因（实测 2 次，usage 均真实计费）
`deepseek-flash` 为推理模型。fixer 提示词（公开代码+测试+标准摘录）下：
  completion_tokens=8000（=授权单请求上限），reasoning_tokens=8000，
  message.content = ""（空）——模型把全部预算耗于内部推理，未产出最终文本。
  首次 max_tokens=4000 同样耗尽（当时误判为"无 diff"）。

## 已用修复轮次（同根因 ≤2 轮）
1. 4000 → 8000（授权上限）：仍耗尽。
2. 提取器/提示词强化 + 失败归档：response 本身为空，非提取问题。

## 需要用户决策（三选一）
a) 提高单请求输出上限（如 16000/32000）——需修改本轮模型预算授权；
b) 更换 fixer/verifier 模型（目录内可选 deepseek-v4-pro 等）——需扩 D-C A2 模型范围；
c) 接受现状，CL-08 保持 BLOCKED，正式验收单按"fixer 阶段未验证"出具。

## 已消耗预算（保守计账）
- 请求：6 次（含 1 次被宿主超时杀灭、1 次 transport 未达=未计费、2 次 reasoning 耗尽、其余为探针）
- 计费：账本保守入账 ≈ 58,770 + 8,800(gap) ≈ 67,570 tokens（上限 200,000 未触）
- 在途请求：无

"""授权前准备包(integration_prep):R1/R2/R3 执行计划、证据采集、R4/R7 同步计划。

**硬边界**:
- 所有脚本默认 dry-run:只打印计划,不执行任何动作;
- 真实执行需同时满足:环境变量 MERGEPILOT_IT_AUTH=1(用户书面授权的机器可读形式)
  + 调用方显式传 execute=True;
- 本包不包含任何真实执行逻辑的触发器;R1/R2/R3 的实际操作仍按
  AUTH-DECISION-PACKAGE.md 逐项获批后进行。
"""
from .steps import R1_PLAN, R2_PLAN, R3_PLAN, R4_SYNC_PLAN, R7_SYNC_PLAN, print_plan  # noqa: F401
from .collect import collect_evidence, redact  # noqa: F401
from .authgate import assert_authorized, is_authorized  # noqa: F401

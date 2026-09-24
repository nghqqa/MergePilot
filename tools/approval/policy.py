"""审批政策配置(D-1/D-2/D-3 配置化,2026-09-22)。

生产原则:政策未配置 → fail-closed(不静默选默认值)。
隔离测试 → 显式提供 fixture 配置。

用法:
  policy = ApprovalPolicy.from_dict({...})  # 显式配置
  policy.assert_can_enable()                 # 未配置时拒绝
  policy.allows_action("generate_patch")     # D-1
  policy.can_approve("alice", "team/repo")   # D-2
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ApprovalPolicy:
    """D-1/D-2/D-3 配置化审批政策。所有字段必须显式提供,不设默认值。"""
    allowed_actions: frozenset          # D-1: 启用的审批动作子集
    approver_map: Dict[str, List[str]]  # D-2: {repo: [github_login,...]}
    ttl_hours: int                      # D-3: 审批有效期(小时)
    configured: bool = True             # False=未配置(fail-closed)
    policy_version: str = ""            # 空=由内容规范哈希派生(orchestration.policy_fingerprint)

    def allows_action(self, action: str) -> bool:
        return action in self.allowed_actions

    def can_approve(self, principal: str, repo: str) -> bool:
        approvers = self.approver_map.get(repo, [])
        return principal in approvers

    def assert_can_enable(self):
        if not self.configured:
            raise PolicyNotConfigured(
                "D-1/D-2/D-3 未拍板:审批功能保持关闭。"
                "需配置 allowed_actions/approver_map/ttl_hours。")

    def to_dict(self) -> dict:
        return {"allowed_actions": sorted(self.allowed_actions),
                "approver_map": self.approver_map,
                "ttl_hours": self.ttl_hours}


class PolicyNotConfigured(PermissionError):
    """审批政策未配置(fail-closed)。"""


@dataclass(frozen=True)
class UnconfiguredPolicy:
    """空政策:所有操作拒绝。"""
    configured: bool = False
    allowed_actions: frozenset = frozenset()
    approver_map: Dict = field(default_factory=dict)
    ttl_hours: int = 0

    def allows_action(self, action): return False
    def can_approve(self, principal, repo): return False
    def assert_can_enable(self):
        raise PolicyNotConfigured("审批政策未配置(D-1/D-2/D-3)")
    def to_dict(self): return {"configured": False}


def load_policy(env: Dict[str, str] = None) -> ApprovalPolicy:
    """从环境变量加载政策。未设置 → 返回 UnconfiguredPolicy。
    env keys:
      MERGEPILOT_APPROVAL_ACTIONS  = "generate_patch,publish_result"
      MERGEPILOT_APPROVERS         = '{"team/demo":["alice","bob"]}'
      MERGEPILOT_APPROVAL_TTL_H    = "24"
      MERGEPILOT_APPROVAL_POLICY_VERSION = "db-2026-09-24"   # 可选;空=内容哈希
    """
    env = os.environ if env is None else env
    actions_raw = env.get("MERGEPILOT_APPROVAL_ACTIONS", "")
    if not actions_raw:
        return UnconfiguredPolicy()
    actions = frozenset(a.strip() for a in actions_raw.split(",") if a.strip())
    approvers_raw = env.get("MERGEPILOT_APPROVERS", "{}")
    approvers = json.loads(approvers_raw)
    ttl = int(env.get("MERGEPILOT_APPROVAL_TTL_H", "24"))
    return ApprovalPolicy(allowed_actions=actions, approver_map=approvers,
                          ttl_hours=ttl,
                          policy_version=env.get("MERGEPILOT_APPROVAL_POLICY_VERSION", ""))

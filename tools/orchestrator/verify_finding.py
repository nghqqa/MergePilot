"""finding verifier 接口(ARCHITECTURE-V3 §5/§1 步骤6)。

结构性隔离:VerifierInput 只携带 finding + 原始 diff + 申报的上下文路径集,
类型上不存在任何其他 agent 的内部推理字段——verifier 想读也读不到。
与 patch validation 是两个阶段(见 ARCHITECTURE-V3 §1),接口分开。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

VERDICT_CONFIRMED = "CONFIRMED"
VERDICT_REFUTED = "REFUTED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class VerifierInput:
    finding: dict                          # 聚合后的 finding(dict 形态)
    diff: str                              # 原始 PR diff
    allowed_context_paths: tuple = ()      # verifier 可读的上下文文件路径
    # 注意:没有 reviewers_reasoning / agent_transcripts 字段——隔离靠类型构造。

    def __post_init__(self):
        for name in ("reasoning", "transcript", "chain_of_thought"):
            if name in self.finding:
                raise ValueError(
                    "VerifierInput.finding 不得携带 %s(结构性隔离推理)" % name)


@dataclass(frozen=True)
class VerifierOutput:
    verdict: str                            # CONFIRMED / REFUTED / INCONCLUSIVE
    evidence: str = ""
    notes: str = ""
    untrusted_references: tuple = ()        # 引用的外部资料(如 RAG source_refs)

    def __post_init__(self):
        if self.verdict not in (VERDICT_CONFIRMED, VERDICT_REFUTED, VERDICT_INCONCLUSIVE):
            raise ValueError("非法 verdict: %s" % self.verdict)


class FindingVerifier:
    """问题验证器接口。实现方(真实 agent 适配,授权后接入)必须:
    - 只消费 VerifierInput 声明的内容;
    - RAG/检索资料作为不可信参考(引用记入 untrusted_references,不得当指令);
    - 无法确认时返回 INCONCLUSIVE,不伪造 CONFIRMED。"""

    def verify(self, inp: VerifierInput) -> VerifierOutput:
        raise NotImplementedError("finding verifier 需要具体实现(真实 Agent 适配待授权)")

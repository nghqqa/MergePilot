# -*- coding: utf-8 -*-
"""review_outcome — 结构化审查结论合同(review-outcome.v1,2026-09-24)。

架构决策(建票所有权冻结轮):
  - Leader/LLM 不再拥有创建审批票据的决定权;正式票据由控制面(桥)依据
    **结构化、已校验** 的 ReviewOutcome 确定性创建。
  - 三个取数形态,产出同一个 v1 合同对象:
      1) review-outcome.json(首选;leader 按合同写入,桥全量校验);
      2) result.md 报告头固定令牌(仅认 STATUS/SEVERITY/
         HUMAN_VERIFICATION_REQUIRED/CWE 四种精确令牌,缺令牌=失败);
      3) reviewer findings.md 同款令牌(leader 完全卡死时仍可确定性取数)。
  - 红线:不从自由文本回填缺失字段;不猜测 action/finding/绑定字段;
    绑定字段(run/repo/pr/head)唯一来源 = write-once run-manifest。
  - fail-closed:schema 无效 / 旧 head / run 不匹配 / 指纹不符 → 拒绝,
    调用方转为 MANUAL_ATTENTION,绝不降级为"从文本猜一张票"。

确定性指纹:fingerprint = canonical_hash({finding_validation, severity, cwe})
(与 approval.canonical_hash 同算法;本模块零依赖实现,一致性由测试钉死)。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional, Tuple

SCHEMA_VERSION = "review-outcome.v1"

VALIDATIONS_ENUM = ("CONFIRMED", "NOT_CONFIRMED", "INCONCLUSIVE")
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
GATE_SEVERITIES = ("HIGH", "CRITICAL")
VALIDATION_KINDS = ("poc",)

OUTCOME_JSON_PATH = "teams/elemiso-team/shared/projects/%s/review-outcome.json"

_TOP_KEYS = frozenset({
    "schema_version", "run_id", "repo", "pr_number", "head_sha", "base_sha",
    "finding_validation", "findings", "validations", "notes"})
_FINDING_KEYS = frozenset({
    "finding_id", "severity", "cwe", "fingerprint", "recommended_actions"})
_VALIDATION_KEYS = frozenset({"kind", "run_id", "head_sha", "evidence_refs"})

_RE_VERDICT = re.compile(r"\bSTATUS:\s*(FINDING_CONFIRMED|NOT_CONFIRMED|INCONCLUSIVE)\b")
_RE_SEVERITY = re.compile(r"\bSEVERITY:\s*(CRITICAL|HIGH|MEDIUM|LOW)\b")
_RE_HVR = re.compile(r"\bHUMAN_VERIFICATION_REQUIRED:\s*(YES|NO)\b")
_RE_CWE = re.compile(r"\bCWE:\s*(CWE-\d{1,4})\b")
_RE_HEAD = re.compile(r"^[0-9a-f]{40}$")
_RE_REPO = re.compile(r"^[^/\s]+/[^/\s]+$")


def canonical_hash(value: Any) -> str:
    """规范序列化 sha256(键排序、紧凑分隔符;与 approval.canonical_hash 同算法)。"""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def finding_fingerprint(finding_validation: str, severity: Optional[str],
                        cwe: Optional[str]) -> str:
    """规范化结构 → 确定性指纹(severity/cwe 缺省以 None 参与哈希,不静默补文本)。"""
    return canonical_hash({"cwe": cwe, "finding_validation": finding_validation,
                           "severity": severity})


def finding_identity(finding_validation: str, severity: Optional[str],
                     cwe: Optional[str]) -> Tuple[str, str]:
    """(finding_id, fingerprint):同一定规范化结构 → 同一身份(重放稳定)。"""
    fp = finding_fingerprint(finding_validation, severity, cwe)
    return "find-" + fp[:12], fp


# ── 源 1:result.md 报告头固定令牌(回退路径) ──────────────────────────────
_TOKEN_RX = (("verdict", _RE_VERDICT), ("severity", _RE_SEVERITY),
             ("hvr", _RE_HVR), ("cwe", _RE_CWE))


def _collect_tokens(lines):
    """行集合 → 令牌表。唯一可信层=四种固定 KEY:VALUE 令牌;散文不读。
    verdict/severity 必须恰好出现(值唯一);HVR/CWE 可省略;
    同名令牌值不一致 → 冲突失败。"""
    out: Dict[str, str] = {}
    for name, rx in _TOKEN_RX:
        vals = []
        for ln in lines:
            vals.extend(rx.findall(ln))
        if len(set(vals)) > 1:
            return None, "conflicting %s tokens" % name
        if vals:
            out[name] = vals[0]
    for required in ("verdict", "severity"):
        if required not in out:
            return None, "missing %s token" % required
    return out, None


def parse_report_tokens(result_text: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """从 result.md 的 SUMMARY 行提取精确令牌(不扫散文正文)。"""
    if not isinstance(result_text, str) or not result_text.strip():
        return None, "empty result text"
    summary_lines = [ln for ln in result_text.splitlines()
                     if ln.startswith("SUMMARY:")]
    if not summary_lines:
        return None, "no SUMMARY header line"
    return _collect_tokens([summary_lines[0]])


def parse_findings_tokens(findings_text: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """reviewer findings.md(结构化交付物)令牌提取:全文扫描四种固定令牌
    (markdown 加粗形态 `**STATUS: ...**` 同样命中);值冲突 → 失败。"""
    if not isinstance(findings_text, str) or not findings_text.strip():
        return None, "empty findings text"
    return _collect_tokens(findings_text.splitlines())


_VERDICT_MAP = {"FINDING_CONFIRMED": "CONFIRMED",
                "NOT_CONFIRMED": "NOT_CONFIRMED",
                "INCONCLUSIVE": "INCONCLUSIVE"}


def _build_outcome(tokens: Dict[str, str], source: str, manifest: Dict[str, Any],
                   pr_number: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    fv = _VERDICT_MAP[tokens["verdict"]]
    severity = tokens.get("severity")
    cwe = tokens.get("cwe")   # 令牌缺失 → None(不从散文猜 CWE)
    if fv == "CONFIRMED":
        if severity is None:
            return None, "confirmed finding without severity token"
        fid, fp = finding_identity(fv, severity, cwe)
        findings = [{"finding_id": fid, "severity": severity, "cwe": cwe,
                     "fingerprint": fp}]
    else:
        findings = []   # 未确认/不确定:没有可建票的 finding
    outcome = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest.get("run_id"),
        "repo": (manifest.get("code") or {}).get("repo"),
        "pr_number": int(pr_number),
        "head_sha": (manifest.get("code") or {}).get("head_sha"),
        "base_sha": (manifest.get("code") or {}).get("base_sha"),
        "finding_validation": fv,
        "findings": findings,
        "validations": [],          # 报告令牌路径无结构化 PoC 记录 → 恒空
        "outcome_source": source,
    }
    return validate_outcome(outcome, manifest, pr_number)


def build_outcome_from_report(result_text: str, manifest: Dict[str, Any],
                              pr_number: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """报告头令牌 + write-once manifest → ReviewOutcome(绑定字段全部来自 manifest)。"""
    tokens, err = parse_report_tokens(result_text)
    if err:
        return None, err
    return _build_outcome(tokens, "report-header", manifest, pr_number)


def build_outcome_from_findings(findings_text: str, manifest: Dict[str, Any],
                                pr_number: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """reviewer findings.md 令牌 + write-once manifest → ReviewOutcome。
    这是 leader 完全卡死时仍能确定性取数的通道(直接读 reviewer 交付物)。"""
    tokens, err = parse_findings_tokens(findings_text)
    if err:
        return None, err
    return _build_outcome(tokens, "reviewer-findings", manifest, pr_number)


# ── 源 2:review-outcome.json(首选路径,全量校验) ─────────────────────────
def parse_outcome_json(doc_text: str, manifest: Dict[str, Any],
                       pr_number: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """leader 写入的 review-outcome.json → 校验+归一。任何不符 → (None, reason)。"""
    try:
        doc = json.loads(doc_text)
    except Exception:
        return None, "outcome doc not json"
    if not isinstance(doc, dict):
        return None, "outcome doc not object"
    unknown = set(doc) - _TOP_KEYS
    if unknown:
        return None, "unknown top-level fields: %s" % ",".join(sorted(unknown))
    if doc.get("schema_version") != SCHEMA_VERSION:
        return None, "schema_version mismatch"
    if doc.get("finding_validation") not in VALIDATIONS_ENUM:
        return None, "finding_validation invalid"
    if not isinstance(doc.get("findings"), list):
        return None, "findings must be a list"
    findings = []
    for f in doc["findings"]:
        if not isinstance(f, dict) or (set(f) - _FINDING_KEYS):
            return None, "finding object invalid or has unknown fields"
        if not isinstance(f.get("finding_id"), str) or not f["finding_id"].strip():
            return None, "finding_id missing"
        if f.get("severity") not in SEVERITIES:
            return None, "finding severity invalid"
        cwe = f.get("cwe")
        if cwe is not None and not isinstance(cwe, str):
            return None, "finding cwe invalid"
        # 指纹由控制面按规范化结构重算(唯一权威算法);doc 可省略;若携带
        # 则必须与重算一致(不符=tamper/缺陷信号 → fail-closed)。
        fid, fp = finding_identity(doc["finding_validation"], f["severity"], cwe)
        if f.get("fingerprint") is not None and f["fingerprint"] != fp:
            return None, "finding fingerprint mismatch (must be canonical recomputation)"
        if "recommended_actions" in f and not isinstance(f["recommended_actions"], list):
            return None, "recommended_actions must be a list (advisory only)"
        findings.append({"finding_id": fid, "severity": f["severity"],
                         "cwe": cwe, "fingerprint": fp})
    if doc["finding_validation"] == "CONFIRMED" and not findings:
        return None, "CONFIRMED outcome requires at least one finding"
    validations = []
    for v in doc.get("validations") or []:
        if not isinstance(v, dict) or (set(v) - _VALIDATION_KEYS):
            return None, "validation object invalid or has unknown fields"
        if v.get("kind") not in VALIDATION_KINDS:
            return None, "validation kind invalid"
        if not isinstance(v.get("evidence_refs"), list) or not v["evidence_refs"]:
            return None, "validation evidence_refs must be a non-empty list"
        validations.append({"kind": v["kind"], "run_id": v.get("run_id"),
                            "head_sha": v.get("head_sha"),
                            "evidence_refs": list(v["evidence_refs"])})
    outcome = {
        "schema_version": SCHEMA_VERSION,
        "run_id": doc.get("run_id"),
        "repo": doc.get("repo"),
        "pr_number": doc.get("pr_number"),
        "head_sha": doc.get("head_sha"),
        "base_sha": doc.get("base_sha"),
        "finding_validation": doc["finding_validation"],
        "findings": findings,
        "validations": validations,
        "outcome_source": "review-outcome.json",
    }
    return validate_outcome(outcome, manifest, pr_number)


# ── 归一后校验:绑定一致性(唯一信任锚 = write-once manifest) ─────────────
def validate_outcome(outcome: Dict[str, Any], manifest: Dict[str, Any],
                     pr_number: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """绑定校验(全部 fail-closed):run/repo/head 与 manifest 一致、head 40hex。"""
    code = manifest.get("code") or {}
    if outcome.get("run_id") != manifest.get("run_id"):
        return None, "outcome run_id mismatch vs run-manifest"
    if outcome.get("repo") != code.get("repo"):
        return None, "outcome repo mismatch vs run-manifest"
    if not isinstance(pr_number, int) or outcome.get("pr_number") != pr_number:
        return None, "outcome pr_number mismatch"
    head = outcome.get("head_sha")
    if not isinstance(head, str) or not _RE_HEAD.fullmatch(head):
        return None, "outcome head_sha not 40hex"
    if head != code.get("head_sha"):
        return None, "outcome head_sha stale vs run-manifest"
    primary = outcome["findings"][0] if outcome["findings"] else None
    if primary is not None:
        fid, fp = finding_identity(outcome["finding_validation"],
                                   primary["severity"], primary["cwe"])
        # 归一:identity 由规范化结构重算,来源字段仅作展示
        primary["finding_id"], primary["fingerprint"] = fid, fp
    digest = canonical_hash({k: v for k, v in outcome.items() if k != "outcome_digest"})
    outcome["outcome_digest"] = digest
    return outcome, None


def gate_worthy(outcome: Optional[Dict[str, Any]]) -> bool:
    """只有 CONFIRMED + HIGH/CRITICAL 进入人工审批门。"""
    if not outcome or outcome.get("finding_validation") != "CONFIRMED":
        return False
    if not outcome.get("findings"):
        return False
    return outcome["findings"][0].get("severity") in GATE_SEVERITIES


def has_current_run_validation(outcome: Dict[str, Any]) -> bool:
    """是否存在绑定**当前 run/head** 的独立验证记录(结构化证明,非历史 PoC)。"""
    for v in outcome.get("validations") or []:
        if (v.get("run_id") == outcome.get("run_id")
                and v.get("head_sha") == outcome.get("head_sha")):
            return True
    return False

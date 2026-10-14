// benchmarks/review-quality/lib/classify.mjs — 发现五类分类（确定性纯函数）。
//
// 分类依据 rules/classification.json（文件化、可评审、可版本化）+ 样本规格
// case.json 的 evaluation_rules（目标路径 + 缺陷标记 + 期望严重度）。
// 顺序语义：按 rules 数组顺序首条命中生效——正确缺陷优先，其次重复
// （相对同 run 已判正确缺陷），再次规格不明确，再次误报，兜底一般建议。
// 变体感知：fixed（正确版）上命中登记缺陷标记 = 断言了不存在的缺陷 → 误报。
import fs from 'node:fs';
import path from 'node:path';

export const CATEGORIES = Object.freeze(
  ['true_defect', 'duplicate_report', 'false_positive', 'ambiguous_spec', 'general_suggestion'],
);

export function loadRules(benchDir) {
  return JSON.parse(fs.readFileSync(path.join(benchDir, 'rules', 'classification.json'), 'utf8'));
}

const anyMatch = (text, patterns) => (patterns ?? []).some((p) => {
  try { return new RegExp(p, 'i').test(text); } catch { return false; }
});

function pathMatchesTarget(findingPath, targetPath) {
  const p = String(findingPath ?? '').replaceAll('\\\\', '/');
  return p === targetPath || p.endsWith(`/${targetPath}`);
}

/**
 * 分类一个 run 的全部 findings。
 * @param findings  生产形状 finding[]（classify 不修改原数组）
 * @param spec      case.json 解析对象
 * @param rules     classification.json 解析对象
 * @param variant   'defect' | 'fixed'——fixed 上命中缺陷标记按误报计（断言不存在的缺陷）
 * @returns [{ finding_index, category, rule_id, reason, severity_deviation? }]
 */
export function classifyFindings(findings, spec, rules, { variant = 'defect' } = {}) {
  const evalRules = spec?.evaluation_rules ?? {};
  const targetPath = evalRules.target_path ?? '';
  const markers = evalRules.defect_marker_patterns ?? [];
  const params = rules.params ?? {};
  const dupWindow = Number(params.duplicate_line_window ?? 3);
  const out = [];
  const trueDefects = []; // 已判 true_defect 的（供重复判定）——仅 defect 变体会产生

  (findings ?? []).forEach((f, i) => {
    const text = `${f.title ?? ''}\n${f.remediation ?? ''}`;
    const inTarget = pathMatchesTarget(f.path, targetPath);
    const hitsMarker = inTarget && anyMatch(text, markers);
    const claimsDefect = anyMatch(text, params.defect_claim_patterns ?? []);
    const claimsStrongDefect = anyMatch(text, params.strong_defect_patterns ?? []);
    const isAmbiguous = anyMatch(text, params.ambiguity_patterns ?? []);
    const isSuggestionish = anyMatch(text, params.suggestion_patterns ?? []);

    // 规则顺序（rules/classification.json v2）：重复 → 正确缺陷 → 建议 → 歧义 → 误报 → 兜底
    let category; let ruleId; let reason;
    const dupOf = trueDefects.find((t) => t.path === f.path
      && Math.abs((t.line_start ?? 0) - (f.line_start ?? 0)) <= dupWindow
      && t.rule_id === f.rule_id);
    if (dupOf) {
      category = 'duplicate_report'; ruleId = 'CLA-DUPLICATE';
      reason = `与第 ${dupOf.index + 1} 条正确缺陷同位置(±${dupWindow})同类别`;
    } else if (hitsMarker) {
      if (variant === 'defect') {
        category = 'true_defect'; ruleId = 'CLA-TRUE-DEFECT';
        reason = `目标文件 ${targetPath} 上命中登记缺陷标记`;
      } else {
        category = 'false_positive'; ruleId = 'CLA-FALSE-POSITIVE';
        reason = '正确版上断言登记缺陷（缺陷已不存在）——按误报计';
      }
    } else if (isSuggestionish && !claimsStrongDefect) {
      category = 'general_suggestion'; ruleId = 'CLA-SUGGESTION';
      reason = '建议措辞且无强缺陷断言——加固/改进建议（对齐 trial §六先例）';
    } else if (isAmbiguous && !claimsDefect) {
      category = 'ambiguous_spec'; ruleId = 'CLA-AMBIGUOUS';
      reason = '表达对预期行为/规格的不确定，未下缺陷断言';
    } else if (claimsDefect) {
      category = 'false_positive'; ruleId = 'CLA-FALSE-POSITIVE';
      reason = inTarget
        ? '目标文件上的缺陷断言但与登记缺陷标记不符'
        : `缺陷断言指向 diff 外/非目标文件（${f.path}）`;
    } else {
      category = 'general_suggestion'; ruleId = 'CLA-GENERAL';
      reason = '无缺陷断言、无歧义表达、无建议措辞（兜底）';
    }
    if (category === 'true_defect') trueDefects.push({ ...f, index: i });

    const item = { finding_index: i, category, rule_id: ruleId, reason };
    if (category === 'true_defect') {
      const expected = evalRules.expected_severity?.value;
      const got = f.severity;
      const tol = Number(evalRules.severity_tolerance ?? 1);
      const rank = { P0: 0, P1: 1, P2: 2, P3: 3 };
      const dev = rank[got] !== undefined && rank[expected] !== undefined
        ? rank[got] - rank[expected] : null;
      item.expected_severity = expected;
      item.rated_severity = got;
      item.severity_deviation = dev; // 负=评得更重，正=评得更轻
      item.severity_exact = dev === 0;
      item.severity_within_tolerance = dev !== null && Math.abs(dev) <= tol;
    }
    out.push(item);
  });
  return out;
}

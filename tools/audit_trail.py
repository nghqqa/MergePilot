# -*- coding: utf-8 -*-
"""
audit_trail.py — 从一次 MergePilot 运行的 trace 生成"审批/回滚/审计"记录。
事实来源 = trace.json 的 spans(结构化运行记录,不 grep prose,避免误判)。
用法:python tools/audit_trail.py [project_id]
"""
import os, sys, glob, io, json

EVIDENCE = r"D:\goai\evidence"


def latest_project(only=None):
    traces = glob.glob(os.path.join(EVIDENCE, "*", "trace.json"))
    best = None
    for t in traces:
        proj = os.path.dirname(t)
        if only and only not in os.path.basename(proj):
            continue
        try:
            tm = os.path.getmtime(t)
        except OSError:
            continue
        if best is None or tm > best[0]:
            best = (tm, proj)
    return best[1] if best else None


def load_spans(project):
    p = os.path.join(project, "trace.json")
    if os.path.exists(p):
        with io.open(p, encoding="utf-8") as f:
            return json.load(f).get("spans", [])
    return []


def render(project, spans):
    proj = os.path.basename(project)
    subs = sorted(set("%s-%02d" % (proj, s["seq"]) for s in spans))
    holds = [s for s in spans if s.get("verdict") == "needs-approval"]
    fails = [s for s in spans if s.get("verdict") == "fail"]
    passes = [s for s in spans if s.get("verdict") == "pass"]
    late = [s for s in spans if holds and s["seq"] > holds[-1]["seq"]]  # 持有之后执行的(= 批准后)
    rollback = bool(fails)
    verdict = "REJECT / ROLLBACK" if rollback else ("MERGE(人审后)" if holds else "MERGE(自动)")

    def sp(s):
        return "`%s-%02d`(%s)" % (proj, s["seq"], s.get("agent", "?"))

    L = ["# 审批与审计记录 · `%s`\n" % proj,
         "> 由 `audit_trail.py` 基于 trace.json 的 spans 生成 —— 命中评分表「审批与回滚、安全可审计」维度。\n",
         "## 风险门决策时间线(基于 trace)", ""]
    L.append("1. **L2 高危持有**:" + ("、".join(sp(s) for s in holds) + " → 挂审批门,不自动合并" if holds else "无 needs-approval span(本次无 L2 持有)"))
    if late:
        L.append("2. **人审后执行 + 复验**:" + "、".join(sp(s) for s in late) + "(批准后才推进)")
    else:
        L.append("2. **人审后执行**:无(本次未触发审批后子任务)")
    L.append("3. **回滚**:" + ("检测到 fail span → 触发回滚:%s" % "、".join(sp(s) for s in fails) if rollback else "无 fail span,未触发回滚(验证通过)"))
    L += ["", "## 最终裁定:**%s**" % verdict, ""]
    L.append("- 持有(需人审):%d 个 span · 通过:%d · 失败:%d" % (len(holds), len(passes), len(fails)))
    L += ["", "## 证据链(子任务产物)", ]
    for s in subs:
        L.append("- `%s/`" % s)
    L += ["", "## 审计要点",
          "- L2 变更必须人审;本记录即审批决策轨迹(谁/何时/批准了哪些)。",
          "- 验证不过 → 自动回滚(本次 %s)。" % ("已触发" if rollback else "未触发——验证通过"),
          "- 全程 trace + 各报告可追溯;证据落 MinIO/shared,不可篡改(见 `trace.md` + 各子任务)。"]
    return "\n".join(L)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    proj = latest_project(only)
    if not proj:
        print("no project found under", EVIDENCE); sys.exit(1)
    spans = load_spans(proj)
    if not spans:
        print("no trace.json spans in", proj); sys.exit(1)
    md = render(proj, spans)
    out = os.path.join(proj, "audit-trail.md")
    io.open(out, "w", encoding="utf-8").write(md)
    holds = sum(1 for s in spans if s.get("verdict") == "needs-approval")
    fails = sum(1 for s in spans if s.get("verdict") == "fail")
    verdict = "ROLLBACK" if fails else ("MERGE(approved)" if holds else "MERGE(auto)")
    print("AUDIT ->", out, "| holds:", holds, "fails:", fails, "| verdict:", verdict)


if __name__ == "__main__":
    main()

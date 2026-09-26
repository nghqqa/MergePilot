# -*- coding: utf-8 -*-
"""从 PR2 收官轮房间导出生成多 Agent 协作时间线 SVG(评委"像脚本"质疑的回应证据)。"""
import json, time

events = json.load(open("D:/goai/r3work/pr2-final-timeline.json", encoding="utf-8"))
events = [e for e in events if "2026-09-19T08:50" <= e["ts"] <= "2026-09-19T09:14:30"]

agents = ["elemiso-admin", "leader", "reviewer", "fixer", "verifier"]
colors = {"elemiso-admin": "#ea580c", "leader": "#2563eb", "reviewer": "#7c3aed",
          "fixer": "#0891b2", "verifier": "#16a34a"}
labels = {"elemiso-admin": "操作员", "leader": "Leader", "reviewer": "Reviewer",
          "fixer": "Fixer", "verifier": "Verifier"}

def sec(ts): return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
t0, t1 = sec("2026-09-19T08:50:50Z"), sec("2026-09-19T09:14:30Z")

W, LANE_H = 1700, 110
H = 130 + len(agents) * LANE_H + 80
PAD_L, PAD_R = 200, 60

def x(ts):
    return PAD_L + (sec(ts) - t0) / (t1 - t0) * (W - PAD_L - PAD_R)

key_nodes = [
    ("08:50:59", "kickoff 指令", "elemiso-admin"),
    ("08:51:05", "委派 review-1", "leader"),
    ("08:51:23", "Skill 调用(diff/sast/risk)", "reviewer"),
    ("08:51:45", "HIGH/CWE-22 提交", "reviewer"),
    ("08:52:00", "停等人工门", "leader"),
    ("09:11:41", "人工门批准", "elemiso-admin"),
    ("09:11:55", "委派 fix-1", "leader"),
    ("09:12:29", "补丁 sha256 674356fc", "fixer"),
    ("09:12:38", "委派 verify-1", "leader"),
    ("09:12:53", "修复前 3 逃逸向量 200", "verifier"),
    ("09:12:56", "修复后全部 400", "verifier"),
    ("09:13:31", "VERIFIED 终态", "leader"),
]

s = []
s.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="Noto Sans SC, Microsoft YaHei, Arial">' % (W, H, W, H))
s.append('<rect width="%d" height="%d" fill="#f8fbff" rx="20"/>' % (W, H))
s.append('<text x="60" y="48" font-size="26" font-weight="700" fill="#0f172a">PR #2 收官轮 · 真实多 Agent 协作时间线</text>')
s.append('<text x="60" y="78" font-size="15" fill="#475569">run-gh-pr2-65de83d6-085056 · 2026-09-19 · Matrix 团队房+Leader DM 共 48 条锁定事件 · 墙钟 ≈23 分钟(含人工门等待 ≈20 分钟,如实分列)</text>')
s.append('<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="8" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#dc2626"/></marker></defs>')

# 泳道
for i, a in enumerate(agents):
    y = 110 + i * LANE_H
    bg = '#ffffff' if i % 2 == 0 else '#f1f5f9'
    s.append('<rect x="50" y="%d" width="%d" height="%d" rx="14" fill="%s" stroke="#e2e8f0"/>' % (y, W-100, LANE_H-8, bg))
    s.append('<rect x="50" y="%d" width="120" height="%d" rx="14" fill="%s"/>' % (y, LANE_H-8, colors[a]))
    s.append('<text x="110" y="%d" text-anchor="middle" font-size="15" font-weight="700" fill="#fff">%s</text>' % (y + (LANE_H-8)//2 + 5, labels[a]))

# 门等待区间
gx1, gx2 = x("2026-09-19T08:52:03Z"), x("2026-09-19T09:11:41Z")
for i in range(len(agents)):
    y = 110 + i * LANE_H
    s.append('<rect x="%d" y="%d" width="%d" height="%d" fill="#fff7ed" fill-opacity="0.55"/>' % (gx1, y, gx2-gx1, LANE_H-8))
s.append('<text x="%d" y="103" text-anchor="middle" font-size="13" fill="#ea580c" font-weight="600">⏸ 人工门等待(操作员决策 ≈20 分钟)</text>' % ((gx1+gx2)//2))

# 消息点
for e in events:
    a = e["sender"]
    if a not in agents: continue
    i = agents.index(a)
    y = 110 + i * LANE_H + (LANE_H - 8) / 2
    s.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" fill-opacity="0.6"/>' % (x(e["ts"]), y, colors[a]))

# 委派箭头
for ts, src, dst in [("2026-09-19T08:51:05Z","leader","reviewer"),
                     ("2026-09-19T09:11:55Z","leader","fixer"),
                     ("2026-09-19T09:12:38Z","leader","verifier")]:
    xx = x(ts)
    y1 = 110 + agents.index(src) * LANE_H + (LANE_H-8)/2
    y2 = 110 + agents.index(dst) * LANE_H + (LANE_H-8)/2
    s.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#dc2626" stroke-width="2.5" marker-end="url(#arr)"/>' % (xx, y1, xx, y2))

# 关键节点标注(交替上下,折行)
for idx, (ts, label, who) in enumerate(key_nodes):
    xx = x("2026-09-19T" + ts + "Z")
    ai = agents.index(who)
    y_base = 110 + ai * LANE_H + (LANE_H-8)/2
    up = idx % 2 == 0
    ty = y_base - (28 if up else -34)
    s.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/>' % (xx, y_base, xx, ty + (10 if up else -8)))
    s.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="600" fill="#334155">%s %s</text>' % (xx, ty, ts, label))

# 底部时间轴
ax_y = H - 45
s.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#64748b" stroke-width="2"/>' % (PAD_L, ax_y, W-PAD_R, ax_y))
for m in [0, 5, 10, 15, 20, 23]:
    total = 50 + m
    ts = "2026-09-19T%02d:%02d:00Z" % (8 + total//60, total%60)
    xx = x(ts)
    s.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#64748b" stroke-width="2"/>' % (xx, ax_y, xx, ax_y+6))
    s.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="12" fill="#64748b">%02d:%02dZ</text>' % (xx, ax_y+22, 8+total//60, total%60))
s.append('<text x="60" y="%d" font-size="11" fill="#94a3b8">数据源: FINALS-ELEM-PR2-WH-20260919/team-room-messages.json + leader-dm-messages.json (SHA256SUMS 锁定) · 生成: 2026-09-19</text>' % (H-8))
s.append('</svg>')

out = "D:/goai/决赛材料/MergePilot-决赛演示包-20260917/03-PPT/pr2-collaboration-timeline.svg"
open(out, "w", encoding="utf-8", newline="\n").write("\n".join(s))
print("SVG written:", out, "|", len(s), "elements")

# LIMITED_READONLY_USER_ACCEPTANCE — 用户验收报告

基线：staging @ 548f2e1（未回退、零代码修改、零 push、零部署）
范围固定：pilot / speaktype#426 + tizhou#2 / 127.0.0.1:48200 / A 链关 / C 链·embedding·pgvector·model cache·RUN_BINDING_AUTH·Fixer·Verifier 全关 / GitHub 只读

## 一 · 移动端 Drawer ESC 复测

**结论：VERIFIED**（真实 CDP 键盘事件，非合成 KeyboardEvent）

步骤与证据（390×844 视口）：
1. 点击"打开菜单"按钮 → 抽屉展开 ✓、遮罩在位 ✓
2. 发送真实 ESC（cua.keypress CDP 级）→ 等待 2.2s
3. 结果：抽屉 display:none ✓、遮罩移除 ✓、**焦点回到菜单按钮** ✓

早前合成 KeyboardEvent 不触发 antd Drawer 关闭——系合成事件的 isTrusted 限制，
不构成产品缺陷；两条备用关闭路径（关闭按钮✓、遮罩点击✓）先前已验证。

## 二 · fail-closed API 验收（8/8 PASS）

| # | 验证 | 结果 |
|---|---|---|
| F1 | 未认证 401 | ✓ |
| F2 | 未授权仓库 403（repo_not_in_allowlist） | ✓ |
| F3 | 未知 pack 404（不泄露存在性） | ✓ |
| F4 | stale head → gate REFUSE（SKILL_GATE_REFUSED_STALE） | ✓ |
| F4b | 过期 ticket past-due 且 API 不可见 | ✓ |
| F4c | 重复运行行数稳定（4 行） | ✓ |
| F5 | 阶段仅来自权威枚举 | ✓ |
| F6 | BLOCKED/STALE/ACTION_REQUIRED 未显示为 PASSED | ✓ |

## 三 · 重启恢复（三容器同时 restart）

- health=200 ✓
- 旧 session 401（按契约失效）✓
- 持久数据：receipts 4 / tickets 1 / audit 2 —— 与重启前一致 ✓
- MinIO 对象 staging-ops/evidence.json 在位 ✓
- 重登后 POSTGRESQL_LIVE、双仓恢复 ✓
- allowlist 恢复（未授权 403）✓

## 四 · 用户反馈归档

| feedback_id | 页面/操作 | 严重度 | 可复现 | 证据 | 边界影响 | 建议 | 阻塞 |
|---|---|---|---|---|---|---|---|
| UA-FB-01 | 390×844 移动端抽屉 / ESC 键 | P3 | 是（合成事件下不可触发；真实按键正常） | CDP keypress ESC → 抽屉关+遮罩移除+焦点回菜单按钮 | 无 | 无需处理（早前观察为合成事件假象） | 否 |
| UA-FB-02 | /overview 数据来源 | P4 | 是 | source 在 <details> 折叠区内，innerText 检测时不可见——设计要求（机器值入详情），但普通用户需展开才能确认实时性 | 无 | 可考虑在折叠摘要行显示 LIVE 徽章（后续优化） | 否 |

无 P1/P2 级发现。无数据边界影响。无安全阻塞。

## 五 · 停止条件

零触发：无未授权泄露 / stale·过期·无 receipt 均被拒绝 / 无阶段改写 / 持久数据零丢失 / 零 GitHub 调用 / 范围未扩大。

## 判定

**LIMITED_READONLY_USER_ACCEPTANCE_VERIFIED**

（ESC 经真实键盘事件 VERIFIED，无需 UNDETERMINED 后缀。）

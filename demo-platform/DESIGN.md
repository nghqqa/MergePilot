# DESIGN.md — MergePilot Demo Platform (built world, recorded at finish)

## World

**Split Theater** — the case page is a stage, not a dashboard. A giant phase word
owns the first viewport; the DAG hangs beside it as a vertical node column; events
run as a horizontal filmstrip; at the human gate the stage is taken over by an amber
control overlay (the surface's one authored moment). The overview is a marquee: a
two-line thesis and two case billboards whose display words are the case outcomes
(AUTONOMOUS / HUMAN GATE). Audit is evidence rows, not card grids.

Direction: user-locked from surface-scope deal 6/2/5 (seed `abcdd867`,
option `split-theater`), code-led build. Contract embedded as the HTML comment in
`frontend/index.html` (survives build; grep for `abcdd867`).

## Palette

| Token | Value | Role |
|---|---|---|
| `--bg0` | `#07090d` | page ground |
| `--bg1` | `#0b0f16` | rails, panels |
| `--bg2` | `#0f1520` | stage ground |
| `--bg3` | `#131b29` | controls |
| `--line` / `--line-strong` | `#1b2534` / `#2b3b52` | hairline steel borders |
| `--ink` / `--ink-2` / `--ink-3` | `#e8eef7` / `#9fb0c4` / `#74839a` | text steps (≥4.5:1 on ground) |
| `--cyan` | `#2dd4ee` | system accent / progress / brand |
| `--amber` | `#fbbf24` | HUMAN GATE — waiting_human states, takeover |
| `--green` | `#4ade80` | verified / completed |
| `--blue` | `#7cb1fb` | running |
| `--red` | `#f87171` | blocked / failure |

Color strategy: restrained dark console; status hues are semantic, never decorative.
Every state is **text + color** (uppercase mono pill), never color-only.

## Type

- Display voice: **Archivo Variable**, width axis 116–122 (`wdth`), weight 800–900,
  uppercase, used only for phase words, billboards, brand, takeover title.
- Data voice: **JetBrains Mono** 400/700, tabular numerals — timestamps, event ids,
  hashes, Matrix ids, control-bar counters. Mono never appears as body text.
- Body: Archivo variable + system CJK fallback (PingFang SC / Microsoft YaHei).

## Components (system grammar)

- `.stage` / `.stage-grid` — stage panel: left phase word + headline + progress rail,
  right vertical DAG column with node dots and connecting rail.
- `.takeover` — full-stage amber overlay at `gate=required`; clip-path wipe entrance
  (the single authored motion); carries approval actions and the
  `REPLAY ACTION — NO RUNTIME WRITE` note.
- `.film-card` / `.filmstrip` — horizontal event strip; current card expands and is
  cyan-banded (amber-banded when the current event is the approval); future cards dim.
- `.billboard` — overview case panels with outcome words as display type.
- `.pill`, `.chip` — status vocabulary; `.resrisk`, `.audit-line`, `.gate-row`,
  `.probe-table` — evidence rows and tables (full-row tint, no colored left borders).
- Icons: one drawn 16px SVG set (`icons.jsx`), 1.7px stroke; no emoji/glyph icons.

## Motion

One authored moment only: the gate takeover wipe (`clip-path` inset). Progress rail
uses `transform: scaleX`. `prefers-reduced-motion` disables pulse/wipe/spin.

## Browser surfaces (themed)

`::selection` cyan-deep; thin scrollbars in palette; `:focus-visible` 2px cyan ring;
`caret-color` cyan; tabular numerals on all data.

## Responsive

Breakpoints: ≤980px stage collapses to one column; ≤720px page padding tightens;
≤1000px control-bar hints hide. Filmstrip scrolls internally; panels scroll wide
tables internally; `.kv dd` may wrap anywhere. Verified overflow-free at
390 / 768 / 1440 px.

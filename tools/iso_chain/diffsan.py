# -*- coding: utf-8 -*-
"""diffsan — 模型 diff 确定性规范化(不改变增删语义)。

常见病修复: hunk 内空行缺前导空格、@@ 头行数与实际不符。
新文件段(--- /dev/null)不处理。
"""
from __future__ import annotations

import re

_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def sanitize_diff(diff: str) -> str:
    lines = diff.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _HUNK.match(line):
            body_start = i + 1
            body = []
            j = body_start
            while (j < len(lines) and not _HUNK.match(lines[j])
                   and not lines[j].startswith("--- ")
                   and not lines[j].startswith("diff --git")):
                body.append(lines[j])
                j += 1
            fixed = []
            for bl in body:
                if bl.strip() == "":
                    fixed.append("")
                elif bl.startswith(("+", "-", " ")):
                    fixed.append(bl)
                else:
                    fixed.append(" " + bl)
            old_n = sum(1 for b in fixed
                        if b.startswith((" ", "-")) or b.strip() == "")
            new_n = sum(1 for b in fixed
                        if b.startswith((" ", "+")) or b.strip() == "")
            old_start = line.split(" ")[1][1:].split(",")[0]
            new_start = line.split(" ")[3][1:].split(",")[0]
            out.append("@@ -%s,%d +%s,%d @@" % (old_start, old_n,
                                                new_start, new_n))
            out.extend(fixed)
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out) + ("\n" if out else "")

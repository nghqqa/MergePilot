# -*- coding: utf-8 -*-
"""iso_chain.patchwork — 补丁产物与干净 checkout 验证(CL-06)。

产物(全部绑定 run/head/ticket/attempt):
  patch.diff(统一 diff)+ sha256 + 文件清单 + 验证报告。

干净 checkout 验证:git apply --check 于独立工作树;未通过 → 补丁
标记 APPLY_FAILED,不得进入 VERIFIED。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any, Dict, List, Optional


def sha256_text(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def build_artifacts(patch_diff: str, *, run_id: str, head_sha: str,
                    ticket_id: str, attempt: int, report: Dict[str, Any],
                    out_dir: str) -> Dict[str, Any]:
    """生成补丁产物集。返回清单 dict(含 patch sha256 与文件列表)。"""
    os.makedirs(out_dir, exist_ok=True)
    patch_path = os.path.join(out_dir, "patch.diff")
    with open(patch_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(patch_diff if patch_diff.endswith("\n") else patch_diff + "\n")
    digest = sha256_text(patch_diff)
    names = set()
    for ln in patch_diff.splitlines():
        if ln.startswith("--- a/"):
            names.add(ln[len("--- a/"):].split("\t")[0].strip())
        elif ln.startswith("+++ b/"):
            names.add(ln[len("+++ b/"):].split("\t")[0].strip())
    files = sorted(n for n in names if n and n != "/dev/null")
    manifest = {
        "run_id": run_id, "head_sha": head_sha, "ticket_id": ticket_id,
        "attempt": attempt, "patch_sha256": digest,
        "files_changed": files, "report": report,
    }
    with open(os.path.join(out_dir, "patch-manifest.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def apply_check(worktree: str, patch_path: str) -> Dict[str, Any]:
    """git apply --check 于干净工作树(不落地)。返回 {ok, detail}。"""
    r = subprocess.run(["git", "apply", "--check", os.path.abspath(patch_path)],
                       cwd=worktree, capture_output=True, text=True, timeout=60)
    return {"ok": r.returncode == 0, "detail": (r.stderr or "").strip()[:300]}


def apply_patch(worktree: str, patch_path: str) -> Dict[str, Any]:
    r = subprocess.run(["git", "apply", os.path.abspath(patch_path)],
                       cwd=worktree, capture_output=True, text=True, timeout=60)
    return {"ok": r.returncode == 0, "detail": (r.stderr or "").strip()[:300]}

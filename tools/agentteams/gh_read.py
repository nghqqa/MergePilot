#!/usr/bin/env python3
"""gh_read.py — MergePilot Worker 只读 GitHub MCP helper (M8-A2-d).

正式仓库资产：Reviewer/Verifier 经受限 GitHub MCP 读取 PR 元数据、变更
文件与指定 branch 文件。OpenClaw exec 预检要求 python 直跑文件调用，
禁止 bash 包装，因此以 python 标准入口提供。

安全合同：
- repo/pr/branch/path 全部严格校验，默认 deny；
- 仅经 mcporter 调用配置好的 github MCP server（list 形式 argv，无 shell 拼接）；
- 不打印 token/Authorization/DSN 或响应中的敏感字段；
- 超时与响应大小上限；失败非零退出；
- 只读：绝不写入 GitHub。

用法：
  gh_read.py pr    <owner> <repo> <pr_number>
  gh_read.py files <owner> <repo> <pr_number>
  gh_read.py file  <owner> <repo> <path> <branch>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

TOOL = "gh_read"
_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_BYTES = 2_000_000
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_PROTECTED_BRANCHES = {"main", "master"}


def _die(msg: str, code: int = 2) -> "None":
    print("[%s] %s" % (TOOL, msg), file=sys.stderr)
    sys.exit(code)


def _validate_owner_repo(owner: str, repo: str) -> None:
    for name, value in (("owner", owner), ("repo", repo)):
        if not _REPO_RE.fullmatch(value or ""):
            _die("invalid %s: %r" % (name, value[:40]))


def _validate_pr(pr_number: str) -> int:
    try:
        pr = int(pr_number)
    except (TypeError, ValueError):
        _die("pr_number must be a positive integer")
    if pr < 1 or pr > 10**9:
        _die("pr_number out of range")
    return pr


def _validate_branch(branch: str) -> None:
    if not _BRANCH_RE.fullmatch(branch or ""):
        _die("invalid branch")
    if branch in _PROTECTED_BRANCHES:
        _die("protected branch denied: %s" % branch)


def _validate_path(path: str) -> None:
    if not path or len(path) > 1024 or path.startswith(("/", "\\")) \
            or ".." in path.replace("\\", "/").split("/"):
        _die("invalid path")


def _mc(*tool_args: str) -> str:
    """Call the configured github MCP server via mcporter (list argv, no
    shell). Output size is enforced WHILE reading (stream cap kills the
    child immediately); timeouts kill+wait; no orphan subprocesses."""
    try:
        proc = subprocess.Popen(
            ["mcporter", "call", *tool_args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        _die("mcporter not available", 3)
    import time as _time
    deadline = _time.monotonic() + _TIMEOUT_SECONDS
    bufs = {proc.stdout: [], proc.stderr: []}
    total = 0
    for stream in (proc.stdout, proc.stderr):
        import os as _os
        _os.set_blocking(stream.fileno(), False)
    open_streams = {proc.stdout, proc.stderr}
    while open_streams:
        if _time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            _die("MCP call timed out", 4)
        import select as _select
        readable, _, _ = _select.select(list(open_streams), [], [], 0.2)
        if not readable:
            if proc.poll() is not None:
                for st in open_streams:
                    st.close()
                open_streams.clear()
            continue
        for st in readable:
            chunk = st.read(65536)
            if chunk is None or chunk == b"" and proc.poll() is not None:
                open_streams.discard(st)
                st.close()
                continue
            if chunk:
                total += len(chunk)
                if total > _MAX_OUTPUT_BYTES:
                    proc.kill()
                    proc.wait()
                    _die("response exceeds size limit", 6)
                bufs[st].append(chunk)
    rc = proc.wait()
    if rc != 0:
        _die("MCP call failed (rc=%d)" % rc, 5)
    out = b"".join(bufs[proc.stdout]).decode("utf-8", "replace")
    err = b"".join(bufs[proc.stderr]).decode("utf-8", "replace")
    if err.strip():
        out = out + err  # mcporter logs errors to stderr; surface them
    return out


def _redact(text: str) -> str:
    """Strip credential-shaped tokens from anything we print."""
    return re.sub(r"(ghp_|gho_|github_pat_)[A-Za-z0-9_]+", "<redacted>", text)


def _owner_repo(owner: str, repo: str):
    _validate_owner_repo(owner, repo)
    return "owner=%s" % owner, "repo=%s" % repo


def main(argv: list) -> int:
    if len(argv) < 5:
        print(__doc__)
        return 2
    mode = argv[1]
    if mode == "pr":
        owner, repo, pr_number = argv[2], argv[3], argv[4]
        id_args = _owner_repo(owner, repo)
        pr = _validate_pr(pr_number)
        raw = _mc("github.get_pull_request", *id_args, "pull_number=%d" % pr)
        # minimal, stable, secret-free projection
        try:
            doc = json.loads(raw[raw.index("{"):])
            slim = {
                "number": doc.get("number"),
                "state": doc.get("state"),
                "merged": doc.get("merged"),
                "head_ref": (doc.get("head") or {}).get("ref"),
                "base_ref": (doc.get("base") or {}).get("ref"),
            }
        except (ValueError, KeyError):
            slim = {"raw_head": _redact(raw)[:400]}
        print(json.dumps(slim, ensure_ascii=False))
        return 0
    if mode == "files":
        owner, repo, pr_number = argv[2], argv[3], argv[4]
        id_args = _owner_repo(owner, repo)
        pr = _validate_pr(pr_number)
        raw = _mc("github.get_pull_request_files", *id_args, "pull_number=%d" % pr)
        names = re.findall(r'"(?:filename|path)"\s*:\s*"([^"]+)"', raw)
        print(json.dumps({"files": names[:100]}, ensure_ascii=False))
        return 0
    if mode == "file":
        if len(argv) < 6:
            print(__doc__)
            return 2
        owner, repo, path, branch = argv[2], argv[3], argv[4], argv[5]
        id_args = _owner_repo(owner, repo)
        _validate_path(path)
        _validate_branch(branch)
        raw = _mc("github.get_file_contents", *id_args,
                  "path=%s" % path, "ref=%s" % branch)
        content = _redact(raw)
        dest_dir = os.environ.get("MERGEPILOT_REVIEW_DIR", "/tmp/review")
        os.makedirs(dest_dir, exist_ok=True)
        name = os.path.basename(path.replace("\\", "/")) or "target"
        with open(os.path.join(dest_dir, name), "w", encoding="utf-8") as fh:
            fh.write(content)
        print(content)
        print("[%s] saved %s" % (TOOL, os.path.join(dest_dir, name)), file=sys.stderr)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

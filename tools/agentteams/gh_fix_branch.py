#!/usr/bin/env python3
"""gh_fix_branch.py — MergePilot Fixer 受控写入 GitHub MCP helper (M8-A2-d).

正式仓库资产：Fixer 仅在 PR 的当前 head branch 上做单文件最小修复提交。

安全合同：
- 写前确认目标 branch == 该 PR 的当前 head branch；拒绝 base/main/master；
- 读当前 blob SHA → 单文件 CAS 更新（create_or_update_file + sha）；
- 写后重新读取并确认内容一致，未确认即失败；
- 禁止：新建/merge/close PR、删除分支、改仓库设置、跨 repo 写入；
- 全参数严格校验、默认 deny；list argv 无 shell 拼接；超时/大小上限；
- 不打印 token/Authorization/DSN；失败非零退出。

用法：
  gh_fix_branch.py <owner> <repo> <pr_number> <branch> <path> <content_file> <commit_msg>
"""
from __future__ import annotations

import re
import subprocess
import sys

TOOL = "gh_fix_branch"
_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_BYTES = 2_000_000
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_PROTECTED_BRANCHES = {"main", "master"}
_SHA_RE = re.compile(r"SHA:\s*([a-f0-9]{40})")


def _die(msg: str, code: int = 2) -> "None":
    print("[%s] %s" % (TOOL, msg), file=sys.stderr)
    sys.exit(code)


def _validate_common(owner: str, repo: str, pr_number: str, branch: str, path: str):
    for name, value in (("owner", owner), ("repo", repo)):
        if not _REPO_RE.fullmatch(value or ""):
            _die("invalid %s" % name)
    try:
        pr = int(pr_number)
    except (TypeError, ValueError):
        _die("pr_number must be a positive integer")
    if pr < 1 or pr > 10**9:
        _die("pr_number out of range")
    if not _BRANCH_RE.fullmatch(branch or ""):
        _die("invalid branch")
    if branch in _PROTECTED_BRANCHES:
        _die("protected branch denied: %s" % branch)
    if not path or len(path) > 1024 or path.startswith(("/", "\\")) \
            or ".." in path.replace("\\", "/").split("/"):
        _die("invalid path")
    return pr


def _mc(*tool_args: str) -> str:
    """Streaming-size-capped MCP call (shared contract with gh_read):
    kill on overflow/timeout, wait the child, never read unbounded."""
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
    import os as _os
    for stream in (proc.stdout, proc.stderr):
        _os.set_blocking(stream.fileno(), False)
    open_streams = {proc.stdout, proc.stderr}
    import select as _select
    while open_streams:
        if _time.monotonic() > deadline:
            proc.kill(); proc.wait()
            _die("MCP call timed out", 4)
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
                open_streams.discard(st); st.close(); continue
            if chunk:
                total += len(chunk)
                if total > _MAX_OUTPUT_BYTES:
                    proc.kill(); proc.wait()
                    _die("response exceeds size limit", 6)
                bufs[st].append(chunk)
    rc = proc.wait()
    if rc != 0:
        _die("MCP call failed (rc=%d)" % rc, 5)
    out = b"".join(bufs[proc.stdout]).decode("utf-8", "replace")
    err = b"".join(bufs[proc.stderr]).decode("utf-8", "replace")
    if err.strip():
        out = out + err
    return out


def main(argv: list) -> int:
    if len(argv) != 8:
        print(__doc__)
        return 2
    owner, repo, pr_number, branch, path, content_file, msg = argv[1:8]
    pr = _validate_common(owner, repo, pr_number, branch, path)
    if len(msg) < 3 or len(msg) > 200:
        _die("invalid commit message length")
    try:
        with open(content_file, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        _die("cannot read content file", 2)
    if not content or len(content.encode("utf-8")) > 512_000:
        _die("content empty or exceeds limit")

    id_args = ("owner=%s" % owner, "repo=%s" % repo, "pull_number=%d" % pr)

    # 1) confirm target branch == PR current head branch (fail-closed)
    pr_raw = _mc("github.get_pull_request", *id_args)
    m = re.search(r'"ref"\s*:\s*"%s"' % re.escape(branch), pr_raw)
    head_ref = re.search(r'"head"\s*:\s*\{[^}]*?"ref"\s*:\s*"([^"]+)"', pr_raw)
    if not m or not head_ref or head_ref.group(1) != branch:
        _die("branch %r is not the current head of PR #%d (head=%r)" % (
            branch[:40], pr, (head_ref.group(1)[:40] if head_ref else "?")))
    # fork guard: the PR head repo must BE the requested repo
    head_sec = pr_raw[pr_raw.index('"head"'):] if '"head"' in pr_raw else ""
    full = re.search(r'"full_name"\s*:\s*"([^"/]+/[^"]+)"', head_sec)
    want_repo = "%s/%s" % (owner, repo)
    if not full or full.group(1) != want_repo:
        _die("PR #%d head repo %r != requested %r (fork PRs are denied)" % (
            pr, (full.group(1) if full else "?"), want_repo))

    # 2) current blob SHA for CAS
    cur = _mc("github.get_file_contents", "owner=%s" % owner, "repo=%s" % repo,
              "path=%s" % path, "ref=%s" % branch)
    sha_m = _SHA_RE.search(cur)
    if not sha_m:
        _die("no blob SHA found for %s @ %s" % (path[:60], branch[:40]))
    sha = sha_m.group(1)

    # 3) single-file CAS update
    _mc("github.create_or_update_file", "owner=%s" % owner, "repo=%s" % repo,
        "path=%s" % path, "branch=%s" % branch, "message=%s" % msg,
        "sha=%s" % sha, "content=%s" % content)

    # 4) read back and confirm
    back = _mc("github.get_file_contents", "owner=%s" % owner, "repo=%s" % repo,
               "path=%s" % path, "ref=%s" % branch)
    if content.strip() not in back:
        _die("post-write read-back mismatch; fix NOT confirmed")
    print("[%s] confirmed update of %s on %s (base sha %s)" % (
        TOOL, path[:60], branch[:40], sha[:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

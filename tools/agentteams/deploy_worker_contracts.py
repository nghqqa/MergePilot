#!/usr/bin/env python3
"""deploy_worker_contracts.py — MergePilot Worker 合同事务化部署工具 (M8-A2-d).

从仓库内读取 SOUL / Manager 合同 / python helpers，以**事务**方式部署到
HiClab：全量 preflight → 每资产备份 → 部署并逐项 SHA256 验证 → 任一失败
逆序 rollback 并逐项验证恢复 → 全部成功才持久化并报告 applied。

退出码：
  0 成功；3 preflight 失败（未做任何修改）；5 部署失败且 rollback 成功；
  9 部署失败且 rollback 失败（高严重度，需人工介入，备份保留在容器内）。

安全合同：
- 默认 dry-run：零 docker/网络/文件副作用；
- `--apply` 唯一执行门；MinIO 凭据仅经环境变量（绝不进 argv/输出）；
- Manager AGENTS.md 为受管标记块事务合并（纯函数 merge_managed_agents），
  替换前容器内备份、替换后回读校验，失败自动恢复备份；
- Manager 持久化写入 **完整合并后** 的 `hiclaw-storage/manager/AGENTS.md`
  （平台 init 以 mc mirror 从该对象恢复 workspace）；
- 不含 fixture 硬编码；repo 限制由正式 policy/allowlist 控制。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

EXIT_OK = 0
EXIT_PREFLIGHT = 3
EXIT_ROLLED_BACK = 5
EXIT_ROLLBACK_FAILED = 9

_MARK_BEGIN = "# MergePilot Worker/Manager 合同部署区 [M8A2D-BEGIN]"
_MARK_END = "# MergePilot 合同部署区结束 [M8A2D-END]"
_MIN_BASE_BYTES = 64          # an AGENTS.md shorter than this is not sane
_STAGE = "/tmp/m8a2d_stage_%s.md"

MATRIX_CONFIG_PLAN = (
    "显式配置计划（不自动热编辑，人工审查后执行）：\n"
    "  1) Manager 需观察任务房间非 mention 群组消息：将 manager openclaw.json\n"
    "     channels.matrix.groups.*.requireMention 置 false，并把房间成员\n"
    "     （controller/admin/manager/reviewer/fixer/verifier）加入 groupAllowFrom；\n"
    "     修改后 OpenClaw 会自行检测并热加载（dynamic reads）或重启容器生效。\n"
    "  2) Worker 容器需可达 github MCP server（mcporter-servers.json 所指端点）：\n"
    "     docker network connect <mcp-net> hiclaw-worker-<name>（一次性网络接线）。\n"
    "  3) 上游运行时故障的人工恢复（workaround，非自动路径）：重启对应 agent\n"
    "     容器可恢复 Matrix 通道；已消费派单无自动重投递，需恢复性提醒。"
)

# (repo source, MinIO dest, container, container dest, kind)
ASSETS = [
    ("config/souls/reviewer/SOUL.md", "agents/reviewer/SOUL.md",
     "hiclaw-worker-reviewer", "/root/hiclaw-fs/agents/reviewer/SOUL.md", "file"),
    ("config/souls/fixer/SOUL.md", "agents/fixer/SOUL.md",
     "hiclaw-worker-fixer", "/root/hiclaw-fs/agents/fixer/SOUL.md", "file"),
    ("config/souls/verifier/SOUL.md", "agents/verifier/SOUL.md",
     "hiclaw-worker-verifier", "/root/hiclaw-fs/agents/verifier/SOUL.md", "file"),
    ("config/souls/manager-state-machine.md", "manager/AGENTS.md",
     "hiclaw-manager", "/root/manager-workspace/AGENTS.md", "managed"),
    ("tools/agentteams/gh_read.py", "agents/reviewer/skills/gh-mcp/gh_read.py",
     "hiclaw-worker-reviewer",
     "/root/hiclaw-fs/agents/reviewer/skills/gh-mcp/gh_read.py", "file"),
    ("tools/agentteams/gh_read.py", "agents/fixer/skills/gh-mcp/gh_read.py",
     "hiclaw-worker-fixer", "/root/hiclaw-fs/agents/fixer/skills/gh-mcp/gh_read.py", "file"),
    ("tools/agentteams/gh_read.py", "agents/verifier/skills/gh-mcp/gh_read.py",
     "hiclaw-worker-verifier",
     "/root/hiclaw-fs/agents/verifier/skills/gh-mcp/gh_read.py", "file"),
    ("tools/agentteams/gh_fix_branch.py",
     "agents/fixer/skills/gh-mcp/gh_fix_branch.py",
     "hiclaw-worker-fixer",
     "/root/hiclaw-fs/agents/fixer/skills/gh-mcp/gh_fix_branch.py", "file"),
]


class DeployError(Exception):
    def __init__(self, msg, code=EXIT_ROLLED_BACK):
        super().__init__(msg)
        self.code = code


def _sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_local(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


# ── §2 pure merge function ────────────────────────────────────────────────

def merge_managed_agents(existing: str, contract: str) -> str:
    """Pure, fail-closed merge of the managed contract block into an
    existing AGENTS.md. Raises ValueError on any unsafe input/state;
    never modifies bytes outside the managed markers.

    - existing must be non-empty, non-whitespace, NUL-free and at least
      _MIN_BASE_BYTES long (the incident precondition was an emptied file);
    - contract must be non-empty and must not itself contain markers;
    - no markers present        -> append exactly one block;
    - exactly one BEGIN + one END, BEGIN first -> replace the old block;
    - any other marker state (single/duplicate/reversed/nested) -> reject;
    - result keeps marker-external bytes exact and contains exactly one
      BEGIN and one END."""
    if not existing or not existing.strip():
        raise ValueError("existing AGENTS is empty or whitespace-only")
    if "\x00" in existing:
        raise ValueError("existing AGENTS contains NUL")
    if len(existing.encode("utf-8", "replace")) < _MIN_BASE_BYTES:
        raise ValueError("existing AGENTS below minimum plausible length")
    if not contract or not contract.strip():
        raise ValueError("contract is empty")
    if _MARK_BEGIN in contract or _MARK_END in contract:
        raise ValueError("contract must not contain managed markers")

    begins = existing.count(_MARK_BEGIN)
    ends = existing.count(_MARK_END)
    # canonical managed block (byte-stable so repeat applies are idempotent)
    block = "\n%s\n%s\n%s\n" % (_MARK_BEGIN, contract.strip("\n"), _MARK_END)

    if begins == 0 and ends == 0:
        head = existing.rstrip("\n")
        tail = ""
    elif begins == 1 and ends == 1:
        b, e = existing.index(_MARK_BEGIN), existing.index(_MARK_END)
        if b > e:
            raise ValueError("managed markers out of order")
        head = existing[:b].rstrip("\n")
        tail = existing[e + len(_MARK_END):].strip("\n")
    else:
        raise ValueError(
            "unsafe managed marker state (begin=%d end=%d)" % (begins, ends))

    merged = head + block + (tail + "\n" if tail else "")

    if merged.count(_MARK_BEGIN) != 1 or merged.count(_MARK_END) != 1:
        raise ValueError("merged result marker count invalid")
    if not merged.strip():
        raise ValueError("merged result empty")

    def external(text):
        if _MARK_BEGIN not in text or _MARK_END not in text:
            return text
        i = text.index(_MARK_BEGIN)
        j = text.index(_MARK_END) + len(_MARK_END)
        return text[:i] + text[j:]

    # newline-style-only tolerance at the block junction; every other byte
    # outside the markers must be identical
    def _norm(t):
        return re.sub(r"\n+", "\n", t).strip("\n")
    if _norm(external(merged)) != _norm(external(existing)):
        raise ValueError("marker-external content changed")
    return merged


# ── transactional primitives ─────────────────────────────────────────────

def _container_sha(container, path):
    r = _sh(["docker", "exec", container, "sha256sum", path])
    if r.returncode != 0:
        return None
    m = re.match(r"([a-f0-9]{64})", r.stdout or "")
    return m.group(1) if m else None


def _container_read(container, path):
    r = _sh(["docker", "exec", container, "cat", path])
    return (r.returncode == 0), r.stdout or ""


def _container_write_verified(container, dest, content: str, tag: str):
    """docker cp transport + container-side sha verification. Returns the
    container-side backup path of the previous file (or None when the
    target did not exist). Raises DeployError on any failure."""
    stage_host = tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8", newline="")
    stage_host.write(content)
    stage_host.close()
    stage_ctr = _STAGE % tag
    try:
        cp = _sh(["docker", "cp", stage_host.name,
                  "%s:%s" % (container, stage_ctr)])
        if cp.returncode != 0:
            raise DeployError("docker cp stage failed for %s" % dest)
        want = _sha256_bytes(content.encode("utf-8"))
        got = _container_sha(container, stage_ctr)
        if got != want:
            raise DeployError("stage sha mismatch for %s" % dest)
        backup = "/tmp/m8a2d_bak_%s.md" % tag
        prev_sha = _container_sha(container, dest)
        if prev_sha is not None:
            cpb = _sh(["docker", "exec", container, "cp", dest, backup])
            if cpb.returncode != 0 or \
                    _container_sha(container, backup) != prev_sha:
                raise DeployError("backup creation/verify failed for %s" % dest)
        else:
            backup = None
        mv = _sh(["docker", "exec", container, "mv", stage_ctr, dest])
        if mv.returncode != 0:
            raise DeployError("atomic replace failed for %s" % dest)
        if _container_sha(container, dest) != want:
            raise DeployError("post-replace sha mismatch for %s" % dest)
        return backup
    finally:
        os.unlink(stage_host.name)
        _sh(["docker", "exec", container, "rm", "-f", stage_ctr])


def _container_restore(container, dest, backup, expected_sha):
    """Restore dest from backup (or remove when backup is None and the file
    must not exist). Returns True when the restored state verifies."""
    if backup is None:
        r = _sh(["docker", "exec", container, "rm", "-f", dest])
        ok = r.returncode == 0 and _container_sha(container, dest) is None
    else:
        r = _sh(["docker", "exec", container, "cp", backup, dest])
        ok = r.returncode == 0 and \
            _container_sha(container, dest) == expected_sha
    return bool(ok)


class Minio:
    """mc-based object IO inside hiclaw-controller. Credentials only via
    docker exec -e env (never argv)."""

    def __init__(self, user, password):
        self.user, self.password = user, password

    def _mc(self, cmd):
        return _sh(["docker", "exec",
                    "-e", "MU=" + self.user, "-e", "MP=" + self.password,
                    "hiclaw-controller", "bash", "-c",
                    "mc alias set local http://localhost:9000 \"$MU\" "
                    "\"$MP\" >/dev/null 2>&1 && " + cmd])

    def write_verified(self, obj, local_path: Path):
        tmp = "/tmp/m8a2d_minio_%s.md" % _sha256_local(local_path)[:12]
        cp = _sh(["docker", "cp", str(local_path), "hiclaw-controller:" + tmp])
        if cp.returncode != 0:
            return False
        r = self._mc("mc cp \"%s\" \"local/hiclaw-storage/%s\" >/dev/null "
                     "&& mc cat \"local/hiclaw-storage/%s\" | sha256sum" %
                     (tmp, obj, obj))
        _sh(["docker", "exec", "hiclaw-controller", "rm", "-f", tmp])
        want = _sha256_local(local_path)
        return r.returncode == 0 and (r.stdout or "").startswith(want)

    def read_to_host(self, obj, host_path: Path):
        tmp = "/tmp/m8a2d_minio_read_%s.md" % obj.replace("/", "_")
        r = self._mc("mc cp \"local/hiclaw-storage/%s\" \"%s\" >/dev/null" %
                     (obj, tmp))
        if r.returncode != 0:
            return False
        cp = _sh(["docker", "cp", "hiclaw-controller:" + tmp, str(host_path)])
        _sh(["docker", "exec", "hiclaw-controller", "rm", "-f", tmp])
        return cp.returncode == 0

    def exists(self, obj):
        return self._mc("mc stat \"local/hiclaw-storage/%s\" >/dev/null" %
                        obj).returncode == 0

    def delete(self, obj):
        return self._mc("mc rm \"local/hiclaw-storage/%s\" >/dev/null" %
                        obj).returncode == 0


# ── per-asset transactions ───────────────────────────────────────────────

def build_manifest():
    manifest = []
    for src_rel, mio_rel, container, dst, kind in ASSETS:
        src = REPO_ROOT / src_rel
        if not src.is_file():
            raise DeployError("missing repo asset: %s" % src_rel,
                              EXIT_PREFLIGHT)
        manifest.append({
            "repo_source": src_rel, "sha256": _sha256_local(src),
            "minio_dest": "hiclaw-storage/" + mio_rel,
            "container": container, "container_dest": dst, "kind": kind,
        })
    return manifest


def preflight(manifest):
    names = sorted({m["container"] for m in manifest})
    for name in names:
        if _sh(["docker", "inspect", name]).returncode != 0:
            raise DeployError("target container missing: %s" % name,
                              EXIT_PREFLIGHT)
    if _sh(["docker", "inspect", "hiclaw-controller"]).returncode != 0:
        raise DeployError("hiclaw-controller missing", EXIT_PREFLIGHT)


def deploy_asset(item, minio: Minio):
    """One asset transaction. Returns a rollback record. Raises DeployError
    on failure (caller rolls back everything applied this round)."""
    src = REPO_ROOT / item["repo_source"]
    src_sha = item["sha256"]
    container, dest = item["container"], item["container_dest"]
    tag = hashlib.sha256((container + dest).encode()).hexdigest()[:10]
    rec = {"item": item, "container_backup": None, "prev_container_sha": None,
           "minio_backup_host": None, "minio_existed": False}

    prev_sha = _container_sha(container, dest)
    if prev_sha == src_sha:
        rec["container_done"] = "already-current"
    else:
        if item["kind"] == "managed":
            ok, current = _container_read(container, dest)
            if not ok:
                raise DeployError("cannot read %s" % dest)
            contract = src.read_text(encoding="utf-8")
            try:
                merged = merge_managed_agents(current, contract)
            except ValueError as exc:
                raise DeployError("merge refused for %s: %s" % (dest, exc))
            backup = _container_write_verified(container, dest, merged, tag)
            ok2, after = _container_read(container, dest)
            if not ok2 or merge_managed_agents(after, contract) != merged:
                if backup:
                    _container_restore(container, dest, backup, prev_sha)
                raise DeployError("post-merge re-verification failed: %s" % dest)
        else:
            backup = _container_write_verified(
                container, dest, src.read_text(encoding="utf-8"), tag)
        rec["container_backup"] = backup
        rec["prev_container_sha"] = prev_sha
        rec["container_done"] = "applied"

    obj = item["minio_dest"].split("hiclaw-storage/", 1)[1]
    if item["kind"] == "managed":
        ok, full_text = _container_read(container, dest)
        if not ok:
            raise DeployError("cannot read back merged AGENTS for persist")
        fd, persist_name = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(full_text)
        persist = Path(persist_name)
    else:
        persist = src
    try:
        if minio.exists(obj):
            fd, bak_name = tempfile.mkstemp(suffix=".bak")
            os.close(fd)
            bak = Path(bak_name)
            if minio.read_to_host(obj, bak):
                rec["minio_backup_host"] = bak
                rec["minio_existed"] = True
            else:
                bak.unlink()
                raise DeployError("cannot backup minio object %s" % obj)
        if not minio.write_verified(obj, persist):
            raise DeployError("minio persist/verify failed for %s" % obj)
        rec["minio_done"] = True
    finally:
        if item["kind"] == "managed":
            persist.unlink()
    return rec


def rollback_all(records, minio: Minio):
    """Reverse-order rollback with per-item verification. Returns True when
    every rollback verified."""
    ok = True
    for rec in reversed(records):
        item = rec["item"]
        container, dest = item["container"], item["container_dest"]
        if rec.get("container_done") == "applied":
            if not _container_restore(container, dest,
                                      rec["container_backup"],
                                      rec["prev_container_sha"]):
                ok = False
        if rec.get("minio_done"):
            obj = item["minio_dest"].split("hiclaw-storage/", 1)[1]
            if rec["minio_existed"] and rec["minio_backup_host"]:
                if not minio.write_verified(obj, rec["minio_backup_host"]):
                    ok = False
            else:
                if minio.exists(obj) and not minio.delete(obj):
                    ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually deploy (default: dry-run)")
    args = ap.parse_args()

    try:
        manifest = build_manifest()
    except DeployError as exc:
        print("[deploy] preflight: %s" % exc, file=sys.stderr)
        return exc.code
    print(json.dumps({"mode": "apply" if args.apply else "dry-run",
                      "assets": manifest}, indent=2, ensure_ascii=False))
    print(MATRIX_CONFIG_PLAN)

    if not args.apply:
        print("[deploy] dry-run complete; no container/network/GitHub side effects")
        return EXIT_OK

    muser = os.environ.get("HICLAW_MINIO_USER", "")
    mpass = os.environ.get("HICLAW_MINIO_PASS", "")
    if not muser or not mpass:
        print("[deploy] preflight: HICLAW_MINIO_USER/HICLAW_MINIO_PASS required",
              file=sys.stderr)
        return EXIT_PREFLIGHT
    minio = Minio(muser, mpass)

    try:
        preflight(manifest)
    except DeployError as exc:
        print("[deploy] preflight: %s" % exc, file=sys.stderr)
        return exc.code

    applied = []
    for item in manifest:
        try:
            rec = deploy_asset(item, minio)
            applied.append(rec)
        except DeployError as exc:
            print("[deploy] FAILED at %s: %s" % (item["repo_source"], exc),
                  file=sys.stderr)
            if rollback_all(applied, minio):
                print("[deploy] rolled back; container backups retained for "
                      "manual recovery", file=sys.stderr)
                return EXIT_ROLLED_BACK
            print("[deploy] ROLLBACK FAILED — manual recovery required; "
                  "backups retained in containers", file=sys.stderr)
            return EXIT_ROLLBACK_FAILED
    for rec in applied:
        if rec.get("minio_backup_host"):
            rec["minio_backup_host"].unlink()
    print("[deploy] applied %d assets transactionally; per-item SHA verified"
          % len(applied))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

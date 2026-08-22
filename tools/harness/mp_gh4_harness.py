"""M8-GH-4B4 Direction-Aware Hybrid Rewiring Harness (mp-gh4-harness).

Formal, testable, rollback-capable implementation of the rewiring
step the B3 planning contract always referenced but was never
delivered ("operator-authorized script; NOT part of mergepilot CLI
ownership").

Single-authority contracts (imported, never duplicated here):
- HICLAW_ROLE_FREEZE / hiclaw_role_gateway_url  (e2e_executors)
- E2E_MATRIX_SERVER_NAME                        (e2e_foundation)

Commands: inspect (default, read-only) | plan (read-only) | apply
(requires explicit --apply) | verify | rollback | status.

Transaction: read state -> validate 4 roles -> capture before hash +
in-container backup -> persist journal -> apply one role -> verify ->
persist progress -> ... -> verify all four -> generate receipt
atomically -> validate receipt with the PRODUCTION validator ->
commit complete state. Any failure rolls back in reverse order; a
rollback failure NEVER replaces the primary apply error.

Every external operation goes through an injectable adapter. The
harness never reads PAT/PEM files, never touches Matrix membership,
never starts/stops/deletes the old github-mcp, never creates or
renames containers, and never prints token/config bodies (hashes and
gateway URLs only — both are receipt fields by contract).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

_TOOLS_CLI = str(Path(__file__).resolve().parents[1] / "cli")
if _TOOLS_CLI not in sys.path:
    sys.path.insert(0, _TOOLS_CLI)

import e2e_executors as ex                       # noqa: E402
import e2e_foundation as e2f                     # noqa: E402

HARNESS_IDENTITY = "mp-gh4-harness"
ROLES = ("manager", "reviewer", "fixer", "verifier")

#: Upper bound for any receipt file the harness will read back during
#: crash recovery (ownership proof refuses oversized targets instead
#: of loading them).
_RECEIPT_MAX_BYTES = 65536

BACKUP_SUFFIX = ".mp-gh4-bak"

_URL_RE = re.compile(r"https?://[^\"'\s]+")


class HarnessError(Exception):
    """Stable-code error; detail is always sanitized (no config
    bodies, no tokens, no secrets)."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


# ── adapters (all injectable; production defaults hit docker) ─────────────

#: In-container conditional-S3 signer (pure python3 stdlib; the
#: controller has no aws-cli/boto3). Read via stdin, argv:
#: [op, bucket-qualified-key, if-match-etag|"-"]. op in
#: put-absent | put-match | get-match. Credentials are read from the
#: mc alias config IN-PROCESS and never printed. stdout line 1 is
#: JSON {"status": int, "etag": str}; the remainder is the body.
#: Verified against the deployed MinIO 2025-09-07: If-None-Match
#: create and If-Match put/get are ATOMIC and enforced server-side;
#: If-Match on DELETE is silently IGNORED by this server (probed:
#: wrong-etag delete returns 204) — therefore the lock protocol
#: NEVER deletes, it transitions to a tombstone via conditional PUT.
_S3_COND_SCRIPT = r'''
import datetime, hashlib, hmac, json, os, re, sys, urllib.request
import urllib.error

#: Transport contract (R3):
#: - argv = [op, bucket-qualified-key, condition|"-"]; the PROGRAM
#:   rides `python3 -c` (never stdin) and the OBJECT BODY is the
#:   ONLY thing on stdin — the two channels can never be confused.
#: - put-absent sends AND SIGNS `If-None-Match: *` (atomic
#:   create-if-absent); put-match/get-match send AND SIGN
#:   `If-Match: <etag>`. A header that is only signed or only sent
#:   is a construction bug this script refuses to produce: the
#:   conditional header is added to the signed set in one place.
#: - redirects are NEVER followed (no cross-host propagation of the
#:   Authorization header); responses are read with an explicit
#:   bound; the ETag is format-checked before being trusted.
#: - credentials live ONLY in the controller's mc alias config
#:   (path overridable via MC_CONFIG_PATH for byte-equivalence
#:   tests against a local fixture); they never reach stdout,
#:   stderr, argv or exception text.

RESPONSE_LIMIT = 65536
_ETAG_RE = re.compile(r"^[0-9a-f]{32}$")

op, target, ifmatch = sys.argv[1], sys.argv[2], sys.argv[3]
# target arrives mc-style (alias/bucket/key): the HTTP path drops
# the ALIAS segment — the alias is an mc client concept, the S3
# bucket is the second segment
http_key = target.split("/", 1)[1] if "/" in target else target
if op not in ("put-absent", "put-match", "get-match"):
    sys.stderr.write("unknown-op")
    sys.exit(3)
if op == "put-match" and (not ifmatch or ifmatch == "-"):
    sys.stderr.write("etag-required")
    sys.exit(3)

cfg = json.load(open(os.environ.get(
    "MC_CONFIG_PATH", "/root/.mc/config.json")))
a = cfg["aliases"]["hiclaw"]
URL, AK, SK = a["url"].rstrip("/"), a["accessKey"], a["secretKey"]
REGION = a.get("region") or "us-east-1"
body = sys.stdin.buffer.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None          # 301/302/303/307/308 stay errors


_OPENER = urllib.request.build_opener(_NoRedirect)


def fail(code_word):
    sys.stderr.write(code_word)
    sys.exit(4)


def enc(s):
    return "".join(chr(b) if chr(b).isalnum() or chr(b) in "-._~/"
                   else "%%%02X" % b for b in s.encode())


def request(method, key, payload, cond_header):
    now = datetime.datetime.utcnow()
    ad, ds = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = URL.split("://", 1)[1]
    ph = hashlib.sha256(payload).hexdigest()
    hs = {"host": host, "x-amz-content-sha256": ph, "x-amz-date": ad}
    if cond_header is not None:
        name, value = cond_header
        hs[name] = value      # signed AND sent — same dict
    signed = ";".join(sorted(hs))
    ch = "".join("%s:%s\n" % (k, hs[k]) for k in sorted(hs))
    path = "/" + enc(http_key)
    cr = "\n".join([method, path, "", ch, signed, ph])
    scope = "%s/%s/s3/aws4_request" % (ds, REGION)
    sts = "\n".join(["AWS4-HMAC-SHA256", ad, scope,
                     hashlib.sha256(cr.encode()).hexdigest()])

    def h(k, m):
        return hmac.new(k, m.encode(), hashlib.sha256).digest()

    sk = h(h(h(h(("AWS4" + SK).encode(), ds), REGION), "s3"),
           "aws4_request")
    sig = hmac.new(sk, sts.encode(), hashlib.sha256).hexdigest()
    r = urllib.request.Request(URL + path, data=payload or None,
                               method=method)
    r.add_header("Authorization",
                 "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s,"
                 " Signature=%s" % (AK, scope, signed, sig))
    for k, v in hs.items():
        if k != "host":
            r.add_header(k, v)
    try:
        resp = _OPENER.open(r, timeout=10)
    except urllib.error.HTTPError as e:
        # includes 3xx: the no-redirect handler turns them into
        # HTTPError — never followed, never retried
        return e.code, dict(e.headers), b""
    except Exception:
        fail("transport-error")
    hd = dict(resp.headers)
    try:
        cl = int(hd.get("Content-Length") or 0)
    except ValueError:
        cl = -1
    if cl > RESPONSE_LIMIT:
        fail("response-oversized")
    data = resp.read(RESPONSE_LIMIT + 1)
    if len(data) > RESPONSE_LIMIT:
        fail("response-oversized")
    return resp.status, hd, data


def etag_of(hd):
    raw = (hd.get("ETag") or hd.get("etag") or "").strip('"')
    if raw and not _ETAG_RE.match(raw):
        fail("etag-format")
    return raw


if op == "put-absent":
    st, hd, _ = request("PUT", target, body,
                        ("if-none-match", "*"))
elif op == "put-match":
    st, hd, _ = request("PUT", target, body,
                        ("if-match", '"%s"' % ifmatch))
else:
    st, hd, bd = request(
        "GET", target, b"",
        ("if-match", '"%s"' % ifmatch)
        if ifmatch != "-" else None)
# single binary output channel: mixing the buffered text layer with
# direct buffer writes reorders the JSON line behind the body, and
# text-mode newline translation varies by host
sys.stdout.buffer.write(
    json.dumps({"status": st, "etag": etag_of(hd)}).encode("utf-8")
    + b"\n")
if op == "get-match":
    sys.stdout.buffer.write(bd)
sys.stdout.buffer.flush()
'''


def _validate_object_key(key: str, *, expect_prefix=None,
                         role: str = None) -> str:
    """F5: single key-validation authority for EVERY MinIO read/
    write/copy/remove/lock/backup path. Rejects empty keys, leading
    separators, dot segments, doubled separators, control bytes,
    URL schemes, bucket roots, prefix spoofing and role/key
    mismatch before anything reaches the object store."""
    if not isinstance(key, str) or not key:
        raise HarnessError("HARNESS_KEY_INVALID", "empty")
    if key.startswith(("/", "\\")):
        raise HarnessError("HARNESS_KEY_INVALID", "leading-sep")
    if "://" in key:
        raise HarnessError("HARNESS_KEY_INVALID", "url-scheme")
    for seg in key.split("/"):
        if seg in ("", ".", ".."):
            raise HarnessError("HARNESS_KEY_INVALID", "dot-or-empty-seg")
    for ch in key:
        if ord(ch) < 0x20 or ord(ch) == 0x7f:
            raise HarnessError("HARNESS_KEY_INVALID", "control-byte")
    if expect_prefix is not None:
        # segment-boundary prefix match: 'mp-gh4-tx-foreign' must
        # NOT pass for 'mp-gh4-tx', 'agents/fixer2' must NOT pass
        # for 'agents/fixer'
        if not (key == expect_prefix
                or key.startswith(expect_prefix + "/")):
            raise HarnessError("HARNESS_KEY_PREFIX_REFUSED",
                               key.split("/")[0])
    if role is not None:
        if key != ex.hiclaw_role_canonical_key(role):
            raise HarnessError("HARNESS_KEY_ROLE_MISMATCH", role)
    return key


class MinioAdapter:
    """MinIO object operations via the controller's mc CLI (argv
    lists, returncode-checked, metadata-only surfaces; credentials
    live only in the container environment and are never read,
    printed or passed by the harness). Conditional create/replace/
    read ride the in-container stdlib S3 signer whose atomicity was
    probed and proven on the deployed server (§2 matrix)."""

    def __init__(self, docker_executor: Callable):
        self._docker = DockerAdapter(docker_executor)
        self.calls = []          # sanitized argv audit

    def _mc(self, argv, *, check=True, timeout=60, input_bytes=None):
        """mc subcommand with production rc semantics: check=True
        fails closed on rc!=0; check=False paths (exists/list/
        remove) must return the CompletedProcess so callers can
        treat a missing object or an unreachable controller as a
        boolean/empty result, never an opaque apply error."""
        self.calls.append(["mc"] + list(argv))
        argv_full = ["exec", "hiclaw-controller", "mc"] + list(argv)
        if check:
            return self._docker._checked(
                argv_full, input_bytes=input_bytes)
        return self._docker._exec(
            argv_full, check=False, timeout=timeout,
            input_bytes=input_bytes)

    def stat(self, key: str) -> dict:
        """{size, etag, date} metadata; never the body."""
        _validate_object_key(key)
        cp = self._mc(["stat", _bucket(key)])
        text = (cp.stdout or b"").decode("utf-8", "replace")
        out = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            if k == "size":
                out["size"] = v
            elif k == "etag":
                out["etag"] = v
            elif k == "date":
                out["date"] = v
        return out

    def hash_of(self, key: str) -> str:
        """SHA-256 of the object body computed in-process on the
        controller; the body itself never crosses the boundary."""
        _validate_object_key(key)
        cp = self._checked_pipe_hash(key)
        return cp

    def _checked_pipe_hash(self, key: str) -> str:
        # mc cat piped to sha256sum inside the controller: body
        # never enters argv, audit, or stdout
        cp = self._checked_exec(
            ["exec", "hiclaw-controller", "sh", "-c",
             "mc cat %s 2>/dev/null | sha256sum" % _bucket(key)],
            audit=["cat-hash", key],
            code="HARNESS_APPLY_FAILED", detail="mc cat rc!=0")
        out = (cp.stdout or b"").decode("utf-8", "replace").strip()
        return out.split()[0] if out else ""

    def exists(self, key: str) -> bool:
        _validate_object_key(key)
        cp = self._mc(["stat", _bucket(key)], check=False)
        return getattr(cp, "returncode", 0) == 0

    def read_bytes(self, key: str, *, max_bytes=65536,
                   expect_prefix=None) -> bytes:
        """Bounded read used only for structural JSON checks (never
        logged, never returned to callers that print). F7 contract:
        stat BEFORE reading (size/type/oversize refusal), then ONE
        rc-enforceable `mc cat` whose failure is never masked by a
        pipeline tail; short reads and etag drift fail closed."""
        _validate_object_key(key, expect_prefix=expect_prefix)
        info = self.stat(key)
        try:
            size = int(str(info.get("size", "0"))
                       .split()[0].replace("B", "").strip())
        except (ValueError, IndexError):
            raise HarnessError("HARNESS_TX_OBJECT_READ_FAILED",
                               "stat-size") from None
        if size > max_bytes:
            raise HarnessError("HARNESS_TX_OBJECT_READ_OVERSIZED",
                               key.split("/")[-1])
        cp = self._mc(["cat", _bucket(key)])
        body = cp.stdout or b""
        if len(body) != size:
            raise HarnessError("HARNESS_TX_OBJECT_READ_SHORT",
                               "%d!=%d" % (len(body), size))
        return body

    def _checked_exec(self, argv, *, input_bytes=None, audit=None,
                      code=None, detail=None):
        """rc-enforcing exec shared by body-carrying operations: a
        non-zero returncode from mc MUST fail closed (never silently
        continue with an unwritten object or empty body)."""
        if audit is not None:
            self.calls.append(list(audit))
        cp = self._docker._exec(argv, check=True,
                                input_bytes=input_bytes)
        if getattr(cp, "returncode", 0) != 0:
            raise HarnessError(
                code or "HARNESS_APPLY_FAILED",
                detail or ("%s rc=%d" % (argv[0], cp.returncode)))
        return cp

    def put_bytes(self, key: str, data: bytes,
                  expect_prefix=None) -> None:
        """Object write via stdin (body never in argv/audit); a failed
        pipe write raises instead of leaving the old object in place."""
        _validate_object_key(key, expect_prefix=expect_prefix)
        self._checked_exec(
            ["exec", "-i", "hiclaw-controller", "sh", "-c",
             "mc pipe %s" % _bucket(key)],
            input_bytes=data, audit=["put", key],
            code="HARNESS_APPLY_FAILED", detail="mc pipe rc!=0")

    def copy(self, src_key: str, dst_key: str, *,
             dst_prefix=None) -> None:
        _validate_object_key(src_key)
        _validate_object_key(dst_key, expect_prefix=dst_prefix)
        # the deployed mc (RELEASE.2025-08-13) only recognizes `cp`;
        # `copy` is not a command there — the audit records the real
        # verb that executed
        self._mc(["cp", _bucket(src_key), _bucket(dst_key)])

    def remove(self, key: str, *, expect_prefix=None) -> None:
        _validate_object_key(key, expect_prefix=expect_prefix)
        cp = self._mc(["rm", _bucket(key)], check=False)
        if getattr(cp, "returncode", 0) != 0:
            raise HarnessError("HARNESS_TX_OBJECT_REMOVE_FAILED",
                               key.split("/")[-1])

    def list_prefix(self, prefix: str) -> list:
        """Keys under a prefix (safe identifiers only)."""
        if prefix:
            _validate_object_key(prefix)
        cp = self._mc(["ls", "--recursive",
                       _bucket_prefix(prefix)], check=False)
        if getattr(cp, "returncode", 0) != 0:
            return []
        keys = []
        for line in (cp.stdout or b"").decode(
                "utf-8", "replace").splitlines():
            parts = line.split()
            if parts:
                keys.append(parts[-1])
        return keys

    # ── atomic conditional primitives (probed on the deployed
    # server: create-if-absent and replace-if-match enforced; see
    # the lock section for the DeleteObject caveat) ──────────────

    def cond_put_absent(self, key: str, data: bytes, *,
                        expect_prefix=None):
        """PutObject If-None-Match:* — 200 (created, new etag) or
        409/412 (already exists, object untouched)."""
        _validate_object_key(key, expect_prefix=expect_prefix)
        return self._cond_request("put-absent", key, data, None)

    def cond_put_match(self, key: str, data: bytes, etag: str, *,
                       expect_prefix=None):
        """PutObject If-Match:<etag> — 200 (replaced, new etag) or
        409/412 (etag drifted, object untouched)."""
        _validate_object_key(key, expect_prefix=expect_prefix)
        if not etag:
            raise HarnessError("HARNESS_KEY_INVALID", "etag-required")
        return self._cond_request("put-match", key, data, etag)

    def cond_get_match(self, key: str, etag: str = None, *,
                       expect_prefix=None):
        """GetObject [If-Match] — (200, etag, body) | (404/412,
        '', b''). Without an etag it is a plain read."""
        _validate_object_key(key, expect_prefix=expect_prefix)
        return self._cond_request("get-match", key, b"",
                                  etag or "-")

    def _cond_request(self, op, key, data, if_match):
        """One signed conditional request executed by the in-container
        stdlib signer; status/etag/body returned as data, never as
        exceptions (callers decide the semantics)."""
        self.calls.append(["cond-" + op.split("-")[1], key])
        # program rides `-c`; the object BODY is the only stdin
        # payload — the two channels can never be swapped. The
        # program is a ONE-LINE base64 bootstrap decoding the very
        # _S3_COND_SCRIPT constant the byte-equivalence tests
        # import: embedded newlines in a raw multi-KB `-c` argument
        # are corrupted by the wsl.exe -> docker exec argv
        # marshalling, a newline-free single token is not.
        import base64 as _b64
        prog = ("import base64;exec(base64.b64decode('%s'))"
                % _b64.b64encode(
                    _S3_COND_SCRIPT.encode("utf-8")).decode("ascii"))
        argv = ["exec", "-i", "hiclaw-controller", "python3", "-c",
                prog, op, _bucket(key), if_match or "-"]
        cp = self._docker._exec(argv, check=True, input_bytes=data)
        if getattr(cp, "returncode", 0) != 0:
            raise HarnessError("HARNESS_TX_LOCK_UNAVAILABLE",
                               "signer rc=%d"
                               % getattr(cp, "returncode", -1))
        out = cp.stdout or b""
        head, _, body = out.partition(b"\n")
        try:
            meta = json.loads(head.decode("utf-8", "replace"))
        except ValueError:
            raise HarnessError("HARNESS_TX_LOCK_UNAVAILABLE",
                               "signer-output") from None
        return int(meta.get("status", 0)), \
            meta.get("etag", ""), body


def _bucket(key: str) -> str:
    return "hiclaw/hiclaw-storage/" + key


def _bucket_prefix(prefix: str) -> str:
    return "hiclaw/hiclaw-storage/" + prefix


class DockerAdapter:
    """Production adapter: docker inspect/exec via argv lists only
    (never shell=True, never secret-bearing argv)."""

    def __init__(self, docker_executor: Callable):
        self._exec = docker_executor
        self.calls = []          # sanitized argv audit (no secrets)

    def _checked(self, argv, *, input_bytes=None):
        self.calls.append(list(argv))
        cp = self._exec(argv, check=True, input_bytes=input_bytes)
        if getattr(cp, "returncode", 0) != 0:
            raise HarnessError("HARNESS_APPLY_FAILED",
                               "%s rc=%d" % (argv[0], cp.returncode))
        return cp

    def inspect_format(self, name: str, fmt: str) -> str:
        cp = self._exec(["inspect", name, "--format", fmt], check=True)
        return (cp.stdout or b"").decode("utf-8", "replace").strip()

    def read_config(self, container: str, path: str) -> bytes:
        cp = self._checked(["exec", container, "cat", path])
        return cp.stdout or b""

    def write_config(self, container: str, path: str,
                     data: bytes) -> None:
        self._checked(
            ["exec", "-i", container, "sh", "-c",
             "cat > %s" % path],      # path is a frozen constant
            input_bytes=data)

    def sha256_file(self, container: str, path: str) -> str:
        cp = self._exec(["exec", container, "sha256sum", path],
                        check=True)
        out = (cp.stdout or b"").decode("utf-8", "replace").strip()
        return out.split()[0] if out else ""


class AtomicFileWriter:
    """Host-side atomic writer with 0600/ACL-restricted files and
    symlink/reparse/path-escape refusal."""

    @staticmethod
    def _safe_path(path: Path, root: Optional[Path] = None) -> None:
        p = str(path)
        if os.path.islink(p):
            raise HarnessError("HARNESS_REPARSE_REFUSED", path.name)
        try:
            if getattr(path.lstat(), "st_reparse_tag", 0):
                raise HarnessError("HARNESS_REPARSE_REFUSED",
                                   path.name)
        except FileNotFoundError:
            pass          # first write: target not created yet
        if root is not None:
            real = os.path.realpath(p)
            root_real = os.path.realpath(str(root))
            me = os.path.normcase(real)
            rt = os.path.normcase(root_real)
            if not (me == rt or me.startswith(rt + os.sep)):
                raise HarnessError("HARNESS_PATH_ESCAPE_REFUSED",
                                   path.name)

    @classmethod
    def write(cls, path, data: bytes, *, root: Path = None) -> None:
        path = Path(path)
        cls._safe_path(path, root)
        tmp = str(path) + ".harness-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def read(cls, path) -> bytes:
        path = Path(path)
        cls._safe_path(path)
        return path.read_bytes()


    @classmethod
    def write_exclusive(cls, path, data: bytes, *,
                        root: Path = None) -> None:
        """Exclusive-create publish (receipt contract, R3):

        - target absent -> atomic creation (same-dir exclusive temp
          + os.link publish; link fails if ANY competitor created the
          target after our preflight — never overwrites/truncates)
        - target present -> OSError (mapped by the caller to
          HARNESS_RECEIPT_EXISTS)
        - 0600 on success; temp cleaned on any failure
        - journal keeps its overwrite-capable atomic write (untouched)
        """
        path = Path(path)
        cls._safe_path(path, root)
        if path.exists():
            raise FileExistsError(str(path))
        tmp = str(path) + ".harness-xcl"
        # exclusive temp creation in the SAME controlled directory
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            # atomic exclusive publish: hard-link fails with
            # FileExistsError if a competitor won the race
            os.link(tmp, str(path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.unlink(tmp)
        except OSError:
            pass
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _canonical_sha256(receipt: dict) -> str:
    """Production-canonical receipt hash (identical rules to
    ex._compute_receipt_sha256; that helper stays the authority —
    this delegates to it)."""
    return ex._compute_receipt_sha256(receipt)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── read-only state inspection ────────────────────────────────────────────

def _load_journal(journal_path, *, expect_session=None) -> dict:
    journal_path = Path(journal_path)
    try:
        raw = AtomicFileWriter.read(journal_path)
    except OSError:
        raise HarnessError("HARNESS_JOURNAL_ABSENT",
                           journal_path.name) from None
    journal = json.loads(raw.decode("utf-8"))
    if journal.get("ownership") != HARNESS_IDENTITY:
        raise HarnessError("HARNESS_FOREIGN_JOURNAL",
                           "ownership mismatch")
    if expect_session is not None             and journal.get("session") != expect_session:
        raise HarnessError("HARNESS_FOREIGN_JOURNAL", "session mismatch")
    return journal


def _persist_journal(writer: AtomicFileWriter, journal_path: Path,
                     journal: dict, root: Path) -> None:
    journal["updated_utc"] = _now_iso()
    try:
        writer.write(journal_path,
                     json.dumps(journal, indent=1,
                                ensure_ascii=True).encode("utf-8"),
                     root=root)
    except OSError as exc:
        raise HarnessError("HARNESS_JOURNAL_PERSIST_FAILED",
                           type(exc).__name__) from None


def _rewrite_config(config_bytes: bytes, target_url: str) -> bytes:
    """Replace every http(s) gateway URL in the mcporter JSON with
    the frozen E2E gateway URL. Deterministic; body never printed."""
    text = config_bytes.decode("utf-8", "replace")
    new_text = _URL_RE.sub(target_url, text)
    return new_text.encode("utf-8")


def _residue_add(residue, item):
    while residue.count(item) > 1:
        residue.remove(item)
    if item not in residue:
        residue.append(item)


def _residue_remove(residue, item):
    while item in residue:
        residue.remove(item)


def _journal_residue(journal: dict) -> list:
    return list(journal.get("rollback_residue", []))


def _sync_fingerprint(docker: DockerAdapter, minio: MinioAdapter):
    """Read the DEPLOYED sync contract from the production scripts
    (bounded, safe greps; no script bodies printed). Returns a dict
    that must match HICLAW_SYNC_FINGERPRINT_EXPECTED or apply
    fails closed."""
    fp = {}
    worker_excl = _grep_count(
        docker, "hiclaw-worker-fixer",
        "/opt/hiclaw/scripts/worker-entrypoint.sh",
        "exclude..config/mcporter.json")
    fp["worker_push_excludes_mcporter"] = worker_excl > 0
    manager_excl = _grep_count(
        docker, "hiclaw-manager",
        "/opt/hiclaw/scripts/init/start-manager-agent.sh",
        "exclude..config/mcporter.json")
    fp["manager_push_excludes_mcporter"] = manager_excl > 0
    pull_period = _grep_first(
        docker, "hiclaw-worker-fixer",
        "/opt/hiclaw/scripts/worker-entrypoint.sh",
        "sleep 300")
    fp["worker_pull_period_seconds"] = (
        300 if pull_period == "sleep 300" else 0)
    return fp


def _grep_count(docker, container, path, pattern):
    """Count matching lines in a deployed script (safe: count
    only; no script body crosses the boundary)."""
    cp = docker._exec(
        ["exec", container, "grep", "-c", "-E", pattern, path],
        check=False)
    out = (cp.stdout or b"").decode("utf-8", "replace").strip()
    try:
        return int(out.splitlines()[-1]) if out else 0
    except (ValueError, IndexError):
        return 0


def _grep_first(docker, container, path, literal):
    cp = docker._exec(
        ["exec", container, "sh", "-c",
         "grep -oF %s %s 2>/dev/null | head -1"
         % (_shellq(literal), _shellq(path))],
        check=False)
    return (cp.stdout or b"").decode("utf-8", "replace").strip()


def _shellq(text):
    return "'" + text.replace("'", "'\\''") + "'"


def _fingerprint_matches(fp: dict) -> bool:
    for key, expected in ex.HICLAW_SYNC_FINGERPRINT_EXPECTED.items():
        if fp.get(key) != expected:
            return False
    return True


def inspect_roles(docker: DockerAdapter,
                  minio: "MinioAdapter" = None) -> dict:
    """Read-only four-role + canonical + sync-contract inventory."""
    state = {"harness": HARNESS_IDENTITY,
             "observed_utc": _now_iso(), "roles": {},
             "old_github_mcp": {}, "sync_fingerprint": None,
             "legacy_sync_artifacts": []}
    for role in ROLES:
        container, mxid, ip, _path = ex.HICLAW_ROLE_FREEZE[role]
        live_id = docker.inspect_format(container, "{{.Id}}")
        running = docker.inspect_format(
            container, "{{.State.Running}}").lower() == "true"
        live_ip = docker.inspect_format(
            container,
            "{{(index .NetworkSettings.Networks \"hiclaw-net\")"
            ".IPAddress}}")
        live_path = ex.hiclaw_role_live_config_path(role)
        target = ex.hiclaw_role_gateway_url(role)
        entry = {
            "container": container,
            "container_id": live_id,
            "running": running,
            "ip_matches": live_ip == ip,
            "sync_mode": ex.hiclaw_role_sync_mode(role),
            "canonical_key": ex.hiclaw_role_canonical_key(role),
            "live_path": live_path,
            "current_gateway_urls": [],
            "target_gateway_url": target,
            "live_sha256": None,
        }
        if running:
            # config body read ONLY from a running container: a
            # stopped role is reported as such, never an opaque
            # exec error (read-only inventory must stay honest)
            config = docker.read_config(container, live_path)
            entry["current_gateway_urls"] = sorted(set(
                _URL_RE.findall(config.decode("utf-8", "replace"))))
            entry["live_sha256"] = hashlib.sha256(config).hexdigest()
        urls = entry["current_gateway_urls"]
        if minio is not None:
            key = ex.hiclaw_role_canonical_key(role)
            if minio.exists(key):
                entry["canonical_sha256"] = minio.hash_of(key)
                entry["canonical_etag"] = minio.stat(key).get("etag")
            else:
                entry["canonical_sha256"] = None
            entry["already_target"] = (
                target in urls
                and entry.get("canonical_sha256")
                == entry["live_sha256"])
        else:
            entry["already_target"] = target in urls
        state["roles"][role] = entry
    old = state["old_github_mcp"]
    old["container_id"] = docker.inspect_format(
        "github-mcp", "{{.Id}}")
    old["state"] = docker.inspect_format(
        "github-mcp", "{{.State.Status}}")
    old["restart_policy"] = docker.inspect_format(
        "github-mcp", "{{.HostConfig.RestartPolicy.Name}}")
    old["networks"] = sorted(docker.inspect_format(
        "github-mcp",
        "{{range $k, $v := .NetworkSettings.Networks}}"
        "{{$k}} {{end}}").split())
    if minio is not None:
        state["sync_fingerprint"] = _sync_fingerprint(docker, minio)
        state["legacy_sync_artifacts"] = sorted(
            k for k in minio.list_prefix("") if ".mp-gh4-bak" in k)
    return state


def _validate_freeze(state: dict) -> None:
    for role in ROLES:
        info = state["roles"][role]
        if not info["container_id"]:
            raise HarnessError("HARNESS_ROLE_MISSING", role)
        if not info["running"]:
            raise HarnessError("HARNESS_ROLE_MISSING",
                               "%s:not-running" % role)
        if not info["ip_matches"]:
            raise HarnessError("HARNESS_IDENTITY_DRIFT",
                               "%s:ip" % role)


def plan(journal_path, docker: DockerAdapter = None,
         minio: MinioAdapter = None) -> dict:
    """Read-only sanitized direction-aware change plan."""
    docker = docker or DockerAdapter(_default_docker_executor())
    minio = minio or MinioAdapter(_default_docker_executor())
    state = inspect_roles(docker, minio)
    _validate_freeze(state)
    actions = []
    for role in ROLES:
        info = state["roles"][role]
        mode = info["sync_mode"]
        actions.append({
            "role": role,
            "sync_mode": mode,
            "mutation_target": ("live" if mode == "live_to_canonical"
                                else "canonical"),
            "convergence": ("production push -> canonical"
                            if mode == "live_to_canonical"
                            else "production pull -> live"),
            "live_path": info["live_path"],
            "canonical_key": info["canonical_key"],
            "target_gateway": info["target_gateway_url"],
            "current_gateways": info["current_gateway_urls"],
            "noop": info["already_target"],
        })
    fp_ok = _fingerprint_matches(state["sync_fingerprint"] or {})
    legacy = state["legacy_sync_artifacts"]
    return {"command": "plan", "actions": actions,
            "sync_fingerprint_ok": fp_ok,
            "legacy_sync_artifacts": legacy,
            "apply_would_fail_closed": bool(legacy) or not fp_ok,
            "journal_path": str(journal_path),
            "writes_executed": 0}


# ── transaction lock (atomic conditional, tombstone protocol) ────
#
# The deployed MinIO (2025-09-07) enforces PutObject If-None-Match
# and If-Match atomically (probed: concurrent create has exactly one
# winner; wrong-etag PUT/GET fail 412) but silently IGNORES If-Match
# on DeleteObject (wrong-etag delete returns 204). Therefore:
#
# - acquire  = conditional create (If-None-Match:*); a stale
#   RELEASED tombstone may be conditionally taken over (If-Match on
#   the tombstone etag — single winner); a HELD lock always
#   conflicts, regardless of age (no time-based takeover);
# - assert   = conditional read (If-Match our etag) + body
#   session/txid/state verification before EVERY external mutation;
# - release  = conditional PUT writing a 'released' tombstone. The
#   lock object is NEVER deleted — an unconditional delete is the
#   one primitive this server cannot make safe. A foreign/replaced
#   lock (412) is never touched and lands in residue.

LOCK_STATE_HELD = "held"
LOCK_STATE_RELEASED = "released"


def _harness_source_sha() -> str:
    try:
        return hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return ""


def _lock_body(session: str, txid: str, state: str) -> bytes:
    return json.dumps({
        "schema": 1, "rewire_session": session, "txid": txid,
        "created": _now_iso(), "state": state,
        "harness_sha256": _harness_source_sha()},
        sort_keys=True).encode("utf-8")


def _lock_parse(body: bytes):
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _tx_lock(minio: MinioAdapter, session: str,
             txid: str) -> dict:
    """Atomic acquire. Returns {key, session, txid, etag}. Stable
    codes: HARNESS_TX_LOCK_CONFLICT (held by anyone, or lost race),
    HARNESS_TX_LOCK_UNAVAILABLE (transport/signer failure)."""
    key = "%s/lock" % ex.HICLAW_TX_PREFIX
    body = _lock_body(session, txid, LOCK_STATE_HELD)
    try:
        status, etag, _ = minio.cond_put_absent(
            key, body, expect_prefix=ex.HICLAW_TX_PREFIX)
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError("HARNESS_TX_LOCK_UNAVAILABLE",
                           type(exc).__name__) from None
    if status == 200:
        return _verify_acquired(minio, key, session, txid, etag)
    if status not in (409, 412):
        raise HarnessError("HARNESS_TX_LOCK_UNAVAILABLE",
                           "put status=%d" % status)
    # key exists: recycle ONLY a released tombstone
    status, cur_etag, cur = minio.cond_get_match(
        key, expect_prefix=ex.HICLAW_TX_PREFIX)
    doc = _lock_parse(cur) if status == 200 else None
    if status == 200 and doc and doc.get(
            "state") == LOCK_STATE_RELEASED:
        try:
            status, etag, _ = minio.cond_put_match(
                key, body, cur_etag,
                expect_prefix=ex.HICLAW_TX_PREFIX)
        except OSError as exc:
            raise HarnessError("HARNESS_TX_LOCK_UNAVAILABLE",
                               type(exc).__name__) from None
        if status == 200:
            return _verify_acquired(minio, key, session, txid, etag)
        if status not in (409, 412):
            raise HarnessError("HARNESS_TX_LOCK_UNAVAILABLE",
                               "takeover status=%d" % status)
    holder = "?"
    if doc and isinstance(doc.get("rewire_session"), str):
        holder = doc["rewire_session"][:24] or "unknown"
    raise HarnessError("HARNESS_TX_LOCK_CONFLICT", holder)


def _verify_acquired(minio: MinioAdapter, key: str, session: str,
                     txid: str, etag: str) -> dict:
    """Post-acquire read-back: body/session/txid/state AND etag must
    all match what we just wrote (belt and braces over the atomic
    create)."""
    status, got_etag, body = minio.cond_get_match(
        key, etag=etag, expect_prefix=ex.HICLAW_TX_PREFIX)
    doc = _lock_parse(body) if status == 200 else None
    if (status != 200 or got_etag != etag or not doc
            or doc.get("rewire_session") != session
            or doc.get("txid") != txid
            or doc.get("state") != LOCK_STATE_HELD):
        raise HarnessError("HARNESS_TX_LOCK_UNAVAILABLE",
                           "verify-readback")
    return {"key": key, "session": session, "txid": txid,
            "etag": etag}


#: Worker on-demand pull primitive (M8-GH-4B6). The deployed
#: worker-entrypoint fallback loop (`sleep 300; mc cp
#: <bucket>/config/mcporter.json <live>`) has NO transaction-level
#: liveness guarantee: its 300s phase is anchored to container
#: start, so a canonical write can fall in a dead zone where no tick
#: fires within any bounded wait (real retry apply: zero pulls in
#: 420s -> HARNESS_WORKER_PULL_CONVERGENCE_TIMEOUT). The harness
#: therefore runs the EXACT same production copy explicitly after
#: each canonical mutation; the 300s loop remains only as a
#: long-term environmental safety net.
_WORKER_PULL_ROLES = ("reviewer", "fixer", "verifier")


def _worker_pull_argv(role: str) -> list:
    """Single source of truth for the on-demand pull argv: container,
    canonical bucket key and live path all come from the frozen
    production authorities (no second role table)."""
    if role not in _WORKER_PULL_ROLES:
        raise HarnessError("HARNESS_WORKER_PULL_TRIGGER_FAILED",
                           "role:%s" % role)
    container = ex.HICLAW_ROLE_FREEZE[role][0]
    key = ex.hiclaw_role_canonical_key(role)
    live = ex.hiclaw_role_live_config_path(role)
    return (container, key, live,
            ["exec", container, "mc", "cp",
             "hiclaw/hiclaw-storage/" + key, live])


def trigger_worker_pull(docker: DockerAdapter, minio: MinioAdapter,
                        role: str) -> None:
    """Explicitly run the production canonical->live copy INSIDE the
    role's container (identical argv to the entrypoint fallback
# loop). rc!=0 or a non-converged read-back fails closed."""
    container, key, live, argv = _worker_pull_argv(role)
    docker.calls.append(["worker-pull", role, container, key, live])
    cp = docker._exec(argv, check=True, timeout=60)
    rc = getattr(cp, "returncode", -1)
    if rc != 0:
        raise HarnessError("HARNESS_WORKER_PULL_TRIGGER_FAILED",
                           "%s rc=%d" % (role, rc))
    live_hash = hashlib.sha256(
        docker.read_config(container, live)).hexdigest()
    if live_hash != minio.hash_of(key):
        raise HarnessError("HARNESS_WORKER_PULL_VERIFY_FAILED", role)


def _assert_lock_owned(minio: MinioAdapter,
                       lock_info: dict) -> None:
    """F2: live lock verification before every external mutation.
    Missing object, etag drift, session/txid/state mismatch or a
    foreign replacement all fail closed."""
    if not lock_info:
        return          # transaction without a lock record (fixture)
    status, etag, body = minio.cond_get_match(
        lock_info["key"], etag=lock_info["etag"],
        expect_prefix=ex.HICLAW_TX_PREFIX)
    doc = _lock_parse(body) if status == 200 else None
    if (status != 200 or etag != lock_info["etag"] or not doc
            or doc.get("rewire_session") != lock_info["session"]
            or doc.get("txid") != lock_info["txid"]
            or doc.get("state") != LOCK_STATE_HELD):
        raise HarnessError("HARNESS_TX_LOCK_LOST",
                           lock_info["key"].split("/")[-1])


def _tx_release(minio: MinioAdapter,
                lock_info: dict) -> str:
    """Atomic, ownership-verified release via conditional tombstone
    PUT. NEVER deletes. Returns 'released' | 'unverified' (foreign
    or replaced lock left untouched) | 'unremovable' (transport)."""
    if not lock_info:
        return "released"
    body = _lock_body(lock_info["session"], lock_info["txid"],
                      LOCK_STATE_RELEASED)
    try:
        status, etag, _ = minio.cond_put_match(
            lock_info["key"], body, lock_info["etag"],
            expect_prefix=ex.HICLAW_TX_PREFIX)
    except (OSError, HarnessError):
        return "unremovable"
    if status == 200:
        lock_info["etag"] = etag
        lock_info["state"] = LOCK_STATE_RELEASED
        return "released"
    if status in (409, 412):
        return "unverified"
    return "unremovable"


# ── direction-aware apply ────────────────────────────────────────────────

def apply(*, journal_path, receipt_path, docker: DockerAdapter = None,
          minio: MinioAdapter = None,
          writer: AtomicFileWriter = None,
          receipt_validator: Callable = None,
          session: str = None,
          phase_hook: Callable = None) -> dict:
    """Direction-aware hybrid transaction:
    manager live->canonical (production push converges canonical),
    workers canonical->live (production pull converges live)."""
    docker = docker or DockerAdapter(_default_docker_executor())
    minio = minio or MinioAdapter(_default_docker_executor())
    writer = writer or AtomicFileWriter()
    # the default session id must satisfy the production receipt
    # validator's contract (^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$); a
    # raw ISO timestamp contains ':' which the validator rejects —
    # sanitize (third real apply failed on exactly this)
    session = session or session_sanitized(
        "rewire-" + _now_iso())
    journal_path = Path(journal_path)
    receipt_path = Path(receipt_path)
    root = journal_path.parent

    def _hook(phase, role):
        if phase_hook is not None:
            phase_hook(phase, role)

    def _persist():
        _persist_journal(writer, journal_path, journal, root)

    state = inspect_roles(docker, minio)
    _validate_freeze(state)

    # fail-closed: sync contract drift
    fp = state["sync_fingerprint"] or {}
    if not _fingerprint_matches(fp):
        raise HarnessError("HARNESS_SYNC_CONTRACT_DRIFT",
                           "deployed scripts differ from frozen "
                           "contract")
    # fail-closed: legacy sync artifacts present
    if state["legacy_sync_artifacts"]:
        raise HarnessError(
            "HARNESS_LEGACY_SYNC_ARTIFACTS_PRESENT",
            "%d objects" % len(state["legacy_sync_artifacts"]))

    # idempotent no-op
    if all(state["roles"][r]["already_target"] for r in ROLES):
        return {"command": "apply", "result": "idempotent-noop",
                "session": session, "receipt": None}

    if journal_path.exists():
        existing = json.loads(
            AtomicFileWriter.read(journal_path).decode("utf-8"))
        if existing.get("ownership") != HARNESS_IDENTITY:
            raise HarnessError("HARNESS_FOREIGN_JOURNAL",
                               "ownership mismatch")
        raise HarnessError("HARNESS_FOREIGN_JOURNAL",
                           "journal exists; run rollback first")

    # receipt ownership preflight: a foreign/pre-existing target at
    # the receipt path fails closed BEFORE lock, journal, backup or
    # any mutation; the foreign bytes are never read or overwritten
    if receipt_path.is_symlink():
        raise HarnessError("HARNESS_REPARSE_REFUSED",
                           receipt_path.name)
    if receipt_path.exists():
        raise HarnessError("HARNESS_RECEIPT_EXISTS",
                           receipt_path.name)

    lock_info = _tx_lock(minio, session, session)
    journal = {"ownership": HARNESS_IDENTITY, "session": session,
               "status": "in-progress", "created_utc": _now_iso(),
               "roles": {},
               "tx_lock": lock_info["key"],
               "tx_lock_session": lock_info["session"],
               "tx_lock_txid": lock_info["txid"],
               "tx_lock_etag": lock_info["etag"],
               "tx_lock_state": "acquired",
               "sync_fingerprint": fp}
    try:
        _persist()
    except HarnessError:
        # the transaction never became durable: tombstone the lock so
        # the fail-closed exit leaves no held lock behind
        _tx_release(minio, lock_info)
        raise

    def _release_lock_into_journal(target_journal=None,
                                   residue=None, diags=None):
        outcome = _tx_release(minio, lock_info)
        book = target_journal if target_journal is not None \
            else journal
        book["tx_lock_etag"] = lock_info["etag"]
        book["tx_lock_state"] = outcome
        if outcome == "unverified":
            if residue is not None:
                _residue_add(residue, "lock:ownership-unverified")
            if diags is not None:
                diags.append("TX_LOCK_OWNERSHIP_UNVERIFIED")
        elif outcome == "unremovable":
            if residue is not None:
                _residue_add(residue, "lock:unremovable")
            if diags is not None:
                diags.append("TX_LOCK_RELEASE_FAILED")
        return outcome

    def _post_rollback_release():
        # the rollback rewrote the DISK journal (honest statuses,
        # residue, diagnostics); reload it before stamping the lock
        # outcome so the persist never resurrects the stale
        # in-progress view
        nonlocal journal
        try:
            journal = json.loads(AtomicFileWriter.read(
                journal_path).decode("utf-8"))
        except (OSError, ValueError):
            journal = {"ownership": HARNESS_IDENTITY,
                       "session": session, "roles": {},
                       "rollback_residue": [],
                       "rollback_diagnostics": []}
        roles_converged = all(
            e.get("status") != "rollback-failed"
            for e in journal.get("roles", {}).values()) and not any(
                d.startswith("TX_LOCK_LOST")
                for d in journal.get("rollback_diagnostics", []))
        if roles_converged:
            _release_lock_into_journal(
                residue=journal.setdefault("rollback_residue", []),
                diags=journal.setdefault("rollback_diagnostics", []))
        else:
            # roles NOT fully converged: the lock STAYS HELD so no
            # new transaction can interleave with an unrestored
            # system; a successful retry will release it
            journal["tx_lock_state"] = "acquired"
        try:
            _persist()
        except HarnessError:
            pass

    before = {}
    backups = {}
    primary = None
    try:
        # ── preflight: consistent before state + session backups ──
        for role in ROLES:
            info = state["roles"][role]
            container = info["container"]
            live_path = info["live_path"]
            key = info["canonical_key"]
            live = docker.read_config(container, live_path)
            live_hash = hashlib.sha256(live).hexdigest()
            canon_hash = minio.hash_of(key)
            etag = minio.stat(key).get("etag", "")
            before[role] = {"live": live_hash,
                            "canonical": canon_hash, "etag": etag}
            if live_hash != canon_hash:
                raise HarnessError(
                    "HARNESS_BEFORE_INCONSISTENT", role)
            target = ex.hiclaw_role_gateway_url(role)
            if target in _URL_RE.findall(
                    live.decode("utf-8", "replace")):
                journal["roles"][role] = {"status": "already-target"}
                _persist()
                continue
            # session-owned backups OUTSIDE all production prefixes.
            # F6 WAL ordering: the backup INTENT is durable BEFORE
            # the external copy runs, so a crash mid-copy is always
            # recoverable from the disk journal (backup_copying).
            bkey = "%s/%s/%s/mcporter.json" % (
                ex.HICLAW_TX_PREFIX, session_sanitized(session), role)
            if minio.exists(bkey):
                raise HarnessError("HARNESS_TX_BACKUP_EXISTS", role)
            journal["roles"][role] = {
                "status": "backup_copying",
                "backup_key": bkey,
                "backup_source": key,
                "before_live": live_hash,
                "before_canonical": canon_hash,
                "before_etag": etag}
            _persist()
            _hook("backup_intent_persisted", role)
            _assert_lock_owned(minio, lock_info)
            minio.copy(key, bkey, dst_prefix=ex.HICLAW_TX_PREFIX)
            _hook("backup_copied", role)
            binfo = minio.stat(bkey)
            if (minio.hash_of(bkey) != canon_hash
                    or binfo.get("etag", "") == ""):
                # our own copy is unusable: remove it BEFORE it
                # becomes an orphan invisible to the journal (the
                # journal entry will be marked failed on rollback)
                try:
                    minio.remove(bkey,
                                 expect_prefix=ex.HICLAW_TX_PREFIX)
                except HarnessError:
                    pass
                raise HarnessError(
                    "HARNESS_TX_BACKUP_VERIFY_FAILED", role)
            backups[role] = bkey
            journal["roles"][role]["backup_etag"] = \
                binfo.get("etag", "")
            journal["roles"][role]["status"] = "pending"
            _persist()
            _hook("backed_up", role)

        def recheck_before(role):
            b = before[role]
            info = state["roles"][role]
            _assert_lock_owned(minio, lock_info)
            if hashlib.sha256(docker.read_config(
                    info["container"],
                    info["live_path"])).hexdigest() != b["live"]:
                raise HarnessError(
                    "HARNESS_EXTERNAL_DRIFT", "%s:live" % role)
            if minio.stat(
                    info["canonical_key"]).get("etag", "")  \
                    not in ("", b["etag"]):
                if minio.hash_of(
                        info["canonical_key"]) != b["canonical"]:
                    raise HarnessError(
                        "HARNESS_EXTERNAL_DRIFT",
                        "%s:canonical" % role)

        # ── manager: live-first, push converges canonical ──
        role = "manager"
        if journal["roles"].get(role, {}).get("status") == "pending":
            _hook("manager_live_applying", role)
            journal["roles"][role]["status"] = "manager_live_applying"
            _persist()
            recheck_before(role)
            info = state["roles"][role]
            target = ex.hiclaw_role_gateway_url(role)
            original = docker.read_config(
                info["container"], info["live_path"])
            new_config = _rewrite_config(original, target)
            docker.write_config(info["container"],
                                info["live_path"], new_config)
            _hook("manager_live_written", role)   # crash window:
            journal["roles"][role]["status"] = "manager_live_mutated"
            _persist()                            # write ran, not durable
            _hook("manager_live_mutated", role)
            live = docker.read_config(info["container"],
                                      info["live_path"])
            if target not in _URL_RE.findall(
                    live.decode("utf-8", "replace")):
                raise HarnessError(
                    "HARNESS_LIVE_WRITE_VERIFY_FAILED", role)
            # bounded wait for production push
            conv = ex.hiclaw_role_convergence(role)
            deadline = time.monotonic() + conv["timeout_seconds"]
            stable = 0
            canon_hash = ""
            while time.monotonic() < deadline:
                time.sleep(conv["poll_seconds"])
                canon_hash = minio.hash_of(
                    info["canonical_key"])
                if canon_hash == hashlib.sha256(live).hexdigest():
                    stable += 1
                    if stable >= conv["stability_checks"]:
                        break
                else:
                    stable = 0
            if stable < conv["stability_checks"]:
                raise HarnessError(
                    "HARNESS_MANAGER_PUSH_CONVERGENCE_TIMEOUT", role)
            journal["roles"][role]["status"] = "manager_converged"
            journal["roles"][role]["after_live"] =  \
                hashlib.sha256(live).hexdigest()
            journal["roles"][role]["after_canonical"] = canon_hash
            journal["roles"][role]["after_etag"] = minio.stat(
                info["canonical_key"]).get("etag", "")
            _persist()
            _hook("manager_converged", role)

        # ── workers: canonical-first, pull converges live ──
        for role in ("reviewer", "fixer", "verifier"):
            if journal["roles"].get(
                    role, {}).get("status") != "pending":
                continue
            _hook("canonical_applying", role)
            journal["roles"][role]["status"] = "canonical_applying"
            _persist()
            recheck_before(role)
            info = state["roles"][role]
            key = info["canonical_key"]
            target = ex.hiclaw_role_gateway_url(role)
            original = minio.read_bytes(key)
            new_obj = _rewrite_config(original, target)
            journal["roles"][role]["status"] = "canonical_mutating"
            minio.put_bytes(key, new_obj)
            _hook("canonical_written", role)     # crash window:
            journal["roles"][role]["status"] = "canonical_mutated"
            _persist()                           # write ran, not durable
            _hook("canonical_mutated", role)
            if minio.hash_of(key) != hashlib.sha256(new_obj).hexdigest():
                raise HarnessError(
                    "HARNESS_CANONICAL_VERIFY_FAILED", role)
            # explicit on-demand production pull: no dependence on
            # the 300s fallback tick's phase (B6)
            journal["roles"][role]["status"] = "worker_pull_triggering"
            _persist()
            _hook("worker_pull_triggering", role)
            _assert_lock_owned(minio, lock_info)
            trigger_worker_pull(docker, minio, role)
            journal["roles"][role]["status"] = "worker_pull_triggered"
            _persist()
            _hook("worker_pull_triggered", role)
            # bounded verification budget only (the pull already
            # ran; this is read-back + stability, not tick waiting)
            deadline = time.monotonic() + 60
            converged = False
            while time.monotonic() < deadline:
                live = docker.read_config(
                    info["container"], info["live_path"])
                if hashlib.sha256(live).hexdigest() \
                        == minio.hash_of(key):
                    time.sleep(1)  # stability re-check
                    live2 = docker.read_config(
                        info["container"], info["live_path"])
                    if hashlib.sha256(live2).hexdigest() \
                            == minio.hash_of(key):
                        converged = True
                        break
            if not converged:
                raise HarnessError(
                    "HARNESS_WORKER_PULL_CONVERGENCE_TIMEOUT", role)
            journal["roles"][role]["status"] = "live_converged"
            journal["roles"][role]["after_canonical"] =  \
                minio.hash_of(key)
            journal["roles"][role]["after_etag"] = minio.stat(
                key).get("etag", "")
            journal["roles"][role]["after_live"] = hashlib.sha256(
                docker.read_config(
                    info["container"],
                    info["live_path"])).hexdigest()
            _persist()
            _hook("live_converged", role)
    except HarnessError as exc:
        primary = exc
    except Exception as exc:
        primary = HarnessError("HARNESS_APPLY_FAILED",
                               type(exc).__name__)

    if primary is not None:
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        primary.diagnostics = rb["diagnostics"]
        raise primary

    if not all(e.get("status") in
               ("manager_converged", "live_converged",
                "already-target")
               for e in journal["roles"].values()):
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        exc = HarnessError("HARNESS_RECEIPT_GENERATION_FAILED",
                           "not all roles converged")
        exc.diagnostics = rb["diagnostics"]
        raise exc

    # receipt: direction-aware fields, canonical hash protected.
    # Exclusive ownership contract (R3/R4): intent is persisted
    # BEFORE the publish so the crash window between publish and the
    # ownership persist is recoverable from the disk journal alone.
    receipt = _build_direction_receipt(state, before, journal,
                                       session)
    receipt_body = json.dumps(receipt, indent=1,
                              ensure_ascii=True).encode("utf-8")
    journal["receipt_state"] = "publishing"
    journal["receipt_path"] = str(receipt_path)
    journal["receipt_session"] = session
    journal["receipt_sha256"] = receipt["receipt_sha256"]
    try:
        _persist()
    except HarnessError:
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        raise
    _hook("receipt_publishing_persisted", None)
    _assert_lock_owned(minio, lock_info)
    try:
        writer.write_exclusive(receipt_path, receipt_body,
                               root=root)
    except FileExistsError:
        # lost the exclusive race to a foreign creator: their bytes
        # stay untouched, everything of ours rolls back
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        raise HarnessError("HARNESS_RECEIPT_EXISTS",
                           receipt_path.name) from None
    except OSError as exc:
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        raise HarnessError("HARNESS_RECEIPT_GENERATION_FAILED",
                           type(exc).__name__) from None
    _hook("receipt_published", None)
    journal["receipt_state"] = "published"
    try:
        _persist()
    except HarnessError:
        try:
            receipt_path.unlink(missing_ok=True)
        except OSError:
            pass
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        raise
    _hook("receipt_ownership_persisted", None)

    # bind the validator to the SAME adapter (injected fakes stay
    # fake; the default binds the real docker executor)
    validator = receipt_validator or _production_validator_with(
        docker, minio)
    try:
        result = validator(str(receipt_path))
    except Exception as exc:
        _unlink_owned(receipt_path)
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        e2 = HarnessError("HARNESS_RECEIPT_VALIDATION_FAILED",
                          type(exc).__name__)
        e2.diagnostics = rb["diagnostics"]
        raise e2 from None
    if not result.get("verified", False):
        _unlink_owned(receipt_path)
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        e2 = HarnessError("HARNESS_RECEIPT_VALIDATION_FAILED",
                          "production validator rejected receipt")
        e2.diagnostics = rb["diagnostics"]
        raise e2

    _assert_lock_owned(minio, lock_info)
    journal["status"] = "complete"
    journal["receipt"] = str(receipt_path)
    journal["receipt_sha256"] = receipt["receipt_sha256"]
    try:
        _persist()
    except HarnessError:
        try:
            receipt_path.unlink(missing_ok=True)
        except OSError:
            pass
        rb = _hybrid_rollback(docker, minio, writer,
                              journal_path, root,
                              lock_info=lock_info)
        _post_rollback_release()
        exc = HarnessError("HARNESS_JOURNAL_PERSIST_FAILED",
                           "complete-stage")
        exc.diagnostics = rb["diagnostics"]
        raise exc
    _hook("complete_persisted", None)

    # cleanup session transaction backups (lock still held: our
    # objects, verified session-owned keys from this transaction)
    _assert_lock_owned(minio, lock_info)
    residue = journal.setdefault("rollback_residue", [])
    for role, bkey in backups.items():
        try:
            minio.remove(bkey,
                         expect_prefix=ex.HICLAW_TX_PREFIX)
        except HarnessError:
            diags = journal.setdefault("rollback_diagnostics", [])
            diags.append("TX_BACKUP_REMOVE_FAILED:%s" % role)
            _residue_add(residue, "tx-backup:%s" % role)
    _release_lock_into_journal(
        residue=residue,
        diags=journal.setdefault("rollback_diagnostics", []))
    if residue:
        journal["status"] = "rollback-residue"
    _persist()
    return {"command": "apply", "result": "complete",
            "session": session,
            "receipt": str(receipt_path),
            "receipt_sha256": receipt["receipt_sha256"]}


def session_sanitized(session: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Za-z0-9._-]", "-", session)[:64]


def _build_direction_receipt(state, before, journal, session):
    agents = []
    for role in ROLES:
        container, mxid, ip, _p = ex.HICLAW_ROLE_FREEZE[role]
        entry = journal["roles"].get(role, {})
        agents.append({
            "role": role,
            "container_name": container,
            "container_id": state["roles"][role]["container_id"],
            "mxid": mxid,
            "hiclaw_net_ip": ip,
            "gateway_url": ex.hiclaw_role_gateway_url(role),
            "sync_mode": ex.hiclaw_role_sync_mode(role),
            "live_path": ex.hiclaw_role_live_config_path(role),
            "canonical_key": ex.hiclaw_role_canonical_key(role),
            "live_hash_before": before[role]["live"],
            "live_hash_after": entry.get("after_live", ""),
            "canonical_hash_before": before[role]["canonical"],
            "canonical_hash_after": entry.get(
                "after_canonical", ""),
            "canonical_etag_before": before[role]["etag"],
            "canonical_etag_after": entry.get("after_etag", ""),
            "convergence_evidence": entry.get("status", ""),
            "config_hash_before": before[role]["live"],
            "config_hash_after": entry.get("after_live", ""),
            "token_hash": hashlib.sha256(
                b"direction-aware").hexdigest(),
        })
    old = state["old_github_mcp"]
    receipt = {
        "schema_version": 2,
        "rewire_session": session,
        "sync_contract_fingerprint": state.get(
            "sync_fingerprint", {}),
        "agents": agents,
        "old_github_mcp": {
            "container_id": old["container_id"],
            "state": old["state"],
            "restart_policy": old["restart_policy"],
            "network_attachments": old["networks"],
        },
        "rollback_ownership": HARNESS_IDENTITY,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return receipt


def _unlink_owned(receipt_path: Path) -> None:
    """Remove THIS session's receipt file only (it was exclusively
    created by us, so ownership is proven by construction). Never
    raises: a failed unlink surfaces via rollback residue instead."""
    try:
        receipt_path.unlink(missing_ok=True)
    except OSError:
        pass


def _prove_receipt_ownership(journal: dict) -> str:
    """Crash-window receipt ownership proof, from the disk journal
    and the receipt FILE alone. Returns 'owned', 'foreign' or
    'absent'. NEVER deletes here — the caller acts on the verdict.

    Proof of ownership (all must hold):
    - the path is not a symlink/reparse point (refuse to follow)
    - the file parses as JSON and is at most _RECEIPT_MAX_BYTES
    - rewire_session == the session persisted in the journal intent
    - the receipt's own receipt_sha256 field matches the canonical
      hash of its body (self-consistent, not tampered)
    - the field equals the journal-recorded receipt_sha256 intent
    """
    path_raw = journal.get("receipt_path")
    if not path_raw:
        return "absent"
    path = Path(path_raw)
    if not path.exists():
        return "absent"
    if path.is_symlink() or os.path.islink(str(path)):
        return "foreign"
    try:
        if path.stat().st_size > _RECEIPT_MAX_BYTES:
            return "foreign"
        receipt = json.loads(
            path.read_bytes()[:_RECEIPT_MAX_BYTES]
            .decode("utf-8", "replace"))
    except (OSError, ValueError):
        return "foreign"
    if not isinstance(receipt, dict):
        return "foreign"
    if receipt.get("rewire_session") != journal.get(
            "receipt_session"):
        return "foreign"
    stored = receipt.get("receipt_sha256", "")
    if stored != _canonical_sha256(receipt):
        return "foreign"
    expected = journal.get("receipt_sha256")
    if expected and stored != expected:
        return "foreign"
    return "owned"


def _journal_lock_info(journal: dict) -> dict:
    """Rebuild the lock ownership record from the DISK journal alone
    (crash recovery). Legacy journals without an etag carry no
    provable ownership and return {} (release becomes a no-op)."""
    if journal.get("tx_lock_etag") and journal.get("tx_lock"):
        return {"key": journal["tx_lock"],
                "session": journal.get("tx_lock_session"),
                "txid": journal.get("tx_lock_txid"),
                "etag": journal["tx_lock_etag"]}
    return {}


def _hybrid_rollback(docker, minio, writer, journal_path, root,
                     lock_info=None):
    """Disk-authoritative direction-aware rollback:
    manager live<-backup then push converges canonical; workers
    canonical<-backup then pull converges live. Retries roles whose
    previous rollback FAILED (R7 §6) and separates config-restore
    failures from backup-cleanup failures honestly (R6). Every
    destructive external operation first re-asserts lock ownership
    (F2): a missing/replaced lock aborts further mutations
    fail-closed instead of continuing unguarded."""
    diags = []
    try:
        journal = json.loads(
            AtomicFileWriter.read(journal_path).decode("utf-8"))
    except (OSError, ValueError):
        return {"rolled_back": [], "diagnostics": ["JOURNAL_UNREADABLE"]}
    if journal.get("ownership") != HARNESS_IDENTITY:
        return {"rolled_back": [], "diagnostics": ["FOREIGN_JOURNAL"]}
    if lock_info is None:
        lock_info = _journal_lock_info(journal)
    rolled = []
    residue = journal.setdefault("rollback_residue", [])
    lock_lost = False

    for role in reversed(ROLES):
        entry = journal["roles"].get(role, {})
        status = entry.get("status", "")
        if not entry:
            # role never reached its WAL persist (crash between the
            # backup intent and the journal entry): nothing was or
            # could have been mutated through the harness
            continue
        if status in ("pending", "already-target", "rolled-back",
                      "rolled-back-with-residue"):
            if status in ("pending", "already-target"):
                entry["status"] = "rolled-back"
                _residue_remove(residue, "role:%s" % role)
            continue
        if status == "backup_copying":
            # F6: the only possible external effect is the backup
            # object itself; the ROLE was never mutated (the copy
            # precedes every mutation). Classified in the cleanup
            # loop below; no restore needed.
            entry["status"] = "rolled-back"
            continue
        if lock_lost:
            # a lost lock must not gate recovery forever: roles are
            # still restored (they undo OUR recorded mutations) but
            # the loss stays visible in diagnostics/residue
            pass
        # statuses reaching here: every mutation-window status AND
        # 'rollback-failed' (retry contract: a restore that failed
        # once is RE-EXECUTED on the next rollback, never skipped)
        container = ex.HICLAW_ROLE_FREEZE[role][0]
        live_path = ex.hiclaw_role_live_config_path(role)
        key = ex.hiclaw_role_canonical_key(role)
        mode = ex.hiclaw_role_sync_mode(role)
        backup_key = entry.get("backup_key")
        try:
            if lock_info:
                _assert_lock_owned(minio, lock_info)
            if not (backup_key and minio.exists(backup_key)):
                # session backup vanished: restore is impossible,
                # fail closed for THIS role only
                raise HarnessError(
                    "HARNESS_TX_BACKUP_MISSING", role)
            before_bytes = minio.read_bytes(
                backup_key, expect_prefix=ex.HICLAW_TX_PREFIX)
            if mode == "live_to_canonical":
                # restore live from tx backup, wait push
                docker.write_config(container, live_path,
                                    before_bytes)
                conv = ex.hiclaw_role_convergence(role)
                deadline = (time.monotonic()
                            + conv["timeout_seconds"])
                stable = 0
                while time.monotonic() < deadline:
                    time.sleep(conv["poll_seconds"])
                    if minio.hash_of(key) \
                            == entry.get("before_canonical"):
                        stable += 1
                        if stable >= 1:
                            break
                    else:
                        stable = 0
                if stable < 1:
                    # canonical did NOT converge back: report the
                    # role as honestly NOT recovered
                    entry["status"] = "rollback-failed"
                    diags.append(
                        "ROLLBACK_CONVERGENCE_FAILED:%s" % role)
                    _residue_add(residue, "canonical:%s" % role)
                    continue
            else:
                # restore canonical from tx backup, then EXPLICITLY
                # trigger the production pull (B6: never depend on
                # the 300s fallback tick to complete a rollback)
                minio.put_bytes(key, before_bytes)
                entry["status"] = "rollback_pull_triggering"
                live_already = hashlib.sha256(
                    docker.read_config(container, live_path)
                    ).hexdigest() == entry.get("before_live")
                try:
                    if not live_already:
                        # live still holds the transaction's target:
                        # converge it back via the production pull
                        _assert_lock_owned(minio, lock_info)
                        trigger_worker_pull(docker, minio, role)
                except HarnessError as exc:
                    if exc.code in ("HARNESS_WORKER_PULL_TRIGGER_FAILED",
                                    "HARNESS_WORKER_PULL_VERIFY_FAILED"):
                        entry["status"] = "rollback-failed"
                        diags.append("ROLLBACK_PULL_TRIGGER_FAILED:%s"
                                     % role)
                        _residue_add(residue, "live:%s" % role)
                        continue
                    raise
                # bounded verification (trigger already converged
                # live; read-back + stability only)
                deadline = time.monotonic() + 60
                ok = False
                while time.monotonic() < deadline:
                    live = docker.read_config(container,
                                              live_path)
                    if hashlib.sha256(live).hexdigest()  \
                            == entry.get("before_live"):
                        ok = True
                        break
                if not ok:
                    # live did NOT converge back: honest failure
                    entry["status"] = "rollback-failed"
                    diags.append(
                        "ROLLBACK_CONVERGENCE_FAILED:%s" % role)
                    _residue_add(residue, "live:%s" % role)
                    continue
            entry["status"] = "rolled-back"
            _residue_remove(residue, "role:%s" % role)
            _residue_remove(residue, "canonical:%s" % role)
            _residue_remove(residue, "live:%s" % role)
            rolled.append(role)
        except HarnessError as exc:
            if exc.code == "HARNESS_TX_LOCK_LOST":
                lock_lost = True
                diags.append("TX_LOCK_LOST:%s" % role)
                _residue_add(residue, "lock:ownership-unverified")
                entry["status"] = "rollback-failed"
                _residue_add(residue, "role:%s" % role)
                continue
            entry["status"] = "rollback-failed"
            diags.append("ROLLBACK_FAILED:%s(%s)"
                         % (role, type(exc).__name__))
            _residue_add(residue, "role:%s" % role)
        except Exception as exc:
            entry["status"] = "rollback-failed"
            diags.append("ROLLBACK_FAILED:%s(%s)"
                         % (role, type(exc).__name__))
            _residue_add(residue, "role:%s" % role)

    # receipt ownership: the crash window between the exclusive
    # publish and the ownership persist leaves a receipt the journal
    # claims as 'publishing'. Delete ONLY what we can prove is ours.
    if journal.get("receipt_state") in ("publishing", "published"):
        verdict = _prove_receipt_ownership(journal)
        if verdict == "owned":
            try:
                Path(journal["receipt_path"]).unlink(missing_ok=True)
            except OSError:
                diags.append("RECEIPT_OWNED_UNLINK_FAILED")
                _residue_add(residue,
                             "receipt:ownership-unverified")
            else:
                journal["receipt_state"] = "deleted"
                _residue_remove(residue,
                                "receipt:ownership-unverified")
        elif verdict == "foreign":
            diags.append("RECEIPT_OWNERSHIP_UNVERIFIED")
            _residue_add(residue, "receipt:ownership-unverified")
        # 'absent': nothing to clean, no residue

    # cleanup tx backups (restore and cleanup are independent: a
    # cleanup failure never marks the RESTORE failed). A role whose
    # restore FAILED keeps its session backup — the retry contract
    # (R7 §6) re-executes the restore from it. F5/F6: deletion only
    # accepts journal-rebuilt keys that RE-VERIFY as session-owned
    # (hash == the recorded before-canonical); anything else is
    # foreign residue and is never deleted.
    for role, e in journal.get("roles", {}).items():
        bkey = e.get("backup_key")
        if not bkey:
            continue
        if e.get("status") == "rollback-failed":
            continue          # kept for the retry; residue role:<r>
        try:
            if minio.exists(bkey):
                try:
                    _validate_object_key(
                        bkey, expect_prefix=ex.HICLAW_TX_PREFIX)
                except HarnessError:
                    diags.append("TX_BACKUP_FOREIGN:%s" % role)
                    _residue_add(residue, "tx-backup-foreign:%s"
                                 % role)
                    continue
                if minio.hash_of(bkey) != e.get("before_canonical"):
                    diags.append("TX_BACKUP_FOREIGN:%s" % role)
                    _residue_add(residue,
                                 "tx-backup-foreign:%s" % role)
                    continue
                if lock_info:
                    _assert_lock_owned(minio, lock_info)
                minio.remove(bkey,
                             expect_prefix=ex.HICLAW_TX_PREFIX)
            _residue_remove(residue, "tx-backup:%s" % role)
            if e.get("status") == "rolled-back-with-residue":
                e["status"] = "rolled-back"
        except HarnessError as exc:
            if exc.code == "HARNESS_TX_LOCK_LOST":
                diags.append("TX_LOCK_LOST:%s" % role)
                _residue_add(residue, "lock:ownership-unverified")
                continue
            _residue_add(residue, "tx-backup:%s" % role)
            diags.append("TX_BACKUP_REMOVE_FAILED:%s" % role)
            if e.get("status") == "rolled-back":
                e["status"] = "rolled-back-with-residue"

    # lock release: conditional tombstone via the journal-rebuilt
    # ownership record. NEVER an unconditional delete; a foreign or
    # replaced lock is left untouched and reported.
    role_failed = any(
        isinstance(e, dict) and e.get("status") == "rollback-failed"
        for e in journal.get("roles", {}).values())
    residue_now = journal.get("rollback_residue", [])
    if role_failed or lock_lost:
        journal["status"] = "rollback-failed"
    elif residue_now:
        journal["status"] = "rollback-residue"
    else:
        journal["status"] = "rolled-back"

    if (lock_info and not role_failed and not lock_lost
            and journal.get("tx_lock_state")
            in (None, "acquired", "unremovable")):
        outcome = _tx_release(minio, lock_info)
        journal["tx_lock_etag"] = lock_info["etag"]
        journal["tx_lock_state"] = outcome
        if outcome == "unverified":
            diags.append("TX_LOCK_OWNERSHIP_UNVERIFIED")
            _residue_add(residue, "lock:ownership-unverified")
        elif outcome == "unremovable":
            diags.append("TX_LOCK_RELEASE_FAILED")
            _residue_add(residue, "lock:unremovable")
        else:
            # retry convergence: a previously unremovable lock
            # residue is cleared exactly once the release lands
            _residue_remove(residue, "lock:unremovable")

    journal["rollback_diagnostics"] = diags
    try:
        _persist_journal(writer, journal_path, journal, root)
    except HarnessError as exc:
        diags.append(exc.code)
    return {"rolled_back": rolled, "diagnostics": diags}


def rollback(*, journal_path, docker: DockerAdapter = None,
             minio: MinioAdapter = None,
             writer: AtomicFileWriter = None,
             session: str = None) -> dict:
    docker = docker or DockerAdapter(_default_docker_executor())
    minio = minio or MinioAdapter(_default_docker_executor())
    writer = writer or AtomicFileWriter()
    journal_path = Path(journal_path)
    journal = _load_journal(journal_path, expect_session=session)
    if journal.get("status") == "complete":
        return {"command": "rollback", "rolled_back": [],
                "residue": [], "note": "journal already complete"}
    result = _hybrid_rollback(docker, minio, writer,
                              journal_path, journal_path.parent)
    hard = [d for d in result["diagnostics"]
            if d.startswith(("ROLLBACK_FAILED",
                             "ROLLBACK_CONVERGENCE_FAILED",
                             "TX_LOCK_LOST"))
            or d == "JOURNAL_UNREADABLE" or d == "FOREIGN_JOURNAL"]
    if hard:
        raise HarnessError("HARNESS_ROLLBACK_FAILED",
                           ";".join(hard))
    journal = json.loads(
        AtomicFileWriter.read(journal_path).decode("utf-8"))
    return {"command": "rollback",
            "rolled_back": result["rolled_back"],
            "residue": journal.get("rollback_residue", []),
            "diagnostics": result["diagnostics"]}


def status(journal_path) -> dict:
    journal = _load_journal(journal_path)
    out = {"command": "status", "ownership": journal["ownership"],
           "session": journal.get("session"),
           "journal_status": journal.get("status"),
           "roles": {r: e.get("status")
                     for r, e in journal.get("roles", {}).items()},
           "residue": _journal_residue(journal),
           "receipt_state": journal.get("receipt_state"),
           "tx_lock_state": journal.get("tx_lock_state")}
    if journal.get("receipt_sha256"):
        out["receipt_sha256"] = journal["receipt_sha256"]
    return out


# ── CLI ───────────────────────────────────────────────────────────────────

def verify(receipt_path, docker: DockerAdapter = None,
           minio: "MinioAdapter" = None) -> dict:
    """Run the PRODUCTION direction-aware validator (read-only)."""
    docker = docker or DockerAdapter(_default_docker_executor())
    minio = minio or MinioAdapter(_default_docker_executor())
    validator = _production_validator_with(docker, minio)
    try:
        result = validator(str(receipt_path))
    except ex.ReceiptValidationError as exc:
        return {"verified": False, "code": exc.code}
    return result


def _default_docker_executor():
    """Real docker via the WSL distro (argv lists, redacted)."""
    def docker_exec(argv, check=True, timeout=60, input_bytes=None,
                    **_):
        cmd = ["wsl", "-d", "Ubuntu-22.04", "-u", "root",
               "--", "docker"] + list(argv)
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            input=input_bytes)
    return docker_exec


def _production_validator_with(docker: DockerAdapter,
                               minio: "MinioAdapter" = None):
    minio = minio or MinioAdapter(_default_docker_executor())

    def validator(receipt_path: str) -> dict:
        return ex.validate_hiclaw_receipt(
            receipt_path,
            docker_executor=docker._exec,
            minio_executor=ex.minio_readonly_via_docker(
                minio._docker._exec),
            expected_old_mcp_state="stopped")
    return validator


def _production_validator(receipt_path: str) -> dict:
    return _production_validator_with(
        DockerAdapter(_default_docker_executor()))(receipt_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mp-gh4-harness",
        description="HiClaw rewiring harness (default command: "
                    "read-only inspect)")
    parser.add_argument("command",
                        choices=["inspect", "plan", "apply", "verify",
                                 "rollback", "status"],
                        nargs="?", default="inspect")
    parser.add_argument("--apply", action="store_true",
                        help="EXPLICIT consent for real modification "
                             "(apply only)")
    parser.add_argument("--journal",
                        default=".mp-gh4-journal.json")
    parser.add_argument("--receipt",
                        default=".mp-gh4-receipt.json")
    args = parser.parse_args(argv)

    docker = DockerAdapter(_default_docker_executor())
    minio = MinioAdapter(_default_docker_executor())
    try:
        if args.command == "inspect":
            print(json.dumps(inspect_roles(docker, minio),
                             indent=1, ensure_ascii=True))
            return 0
        if args.command == "plan":
            print(json.dumps(plan(args.journal, docker, minio),
                             indent=1, ensure_ascii=True))
            return 0
        if args.command == "status":
            print(json.dumps(status(args.journal), indent=1,
                             ensure_ascii=True))
            return 0
        if args.command == "verify":
            result = verify(args.receipt, docker, minio)
            print(json.dumps(result, indent=1, ensure_ascii=True))
            return 0 if result.get("verified") else 1
        if args.command == "rollback":
            print(json.dumps(
                rollback(journal_path=args.journal,
                         docker=docker, minio=minio),
                indent=1, ensure_ascii=True))
            return 0
        if args.command == "apply":
            if not args.apply:
                print("refusing: apply requires explicit --apply")
                return 2
            result = apply(journal_path=args.journal,
                           receipt_path=args.receipt,
                           docker=docker, minio=minio)
            print(json.dumps(result, indent=1, ensure_ascii=True))
            return 0
    except HarnessError as exc:
        print("HARNESS_ERROR %s: %s" % (exc.code, exc.detail))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

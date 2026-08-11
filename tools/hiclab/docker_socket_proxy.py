#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2B-3B1 · Docker Socket Proxy — deny-by-default, fail-closed Unix reverse proxy.

Purpose
-------
AgentTeams v1.2.2 embedded controller mounts /var/run/docker.sock directly
(install.sh:4015) and talks to the Docker Engine API over a Unix socket via
net.Dial("unix", SocketPath) (docker.go:45). This proxy owns a filtered socket
at /run/mp/docker.sock; the controller's AGENTTEAMS_PROXY_SOCKET is pointed at
the in-container /var/run/docker.sock which deploy maps to the proxy socket.
The proxy forwards only the 13 SOURCE_PROVEN endpoints the controller actually
needs (verified from docker.go line-by-line at commit 849182a) and DENIES
everything else.

Trust boundary
--------------
- The proxy is the ONLY Docker API surface the controller can reach.
- All decisions are deny-by-default: any endpoint, method, query, or body
  field not explicitly allowed is 403.
- The proxy never opens the raw /var/run/docker.sock to the controller; it
  parses, validates, optionally transforms, then re-issues the request itself.
- On ANY error (upstream disconnect, parse failure, timeout, size limit), the
  proxy returns fail-closed (403 or 502) — never degrades to passthrough.

Status: design-frozen (D2B-3A.1 v2). This module IS the implementation, but
until deployed + audited + marker written, hiclaw_live remains false and
UPSTREAM_BLOCKED.md stays BLOCKED_UPSTREAM.

This module contains NO Docker/network calls at import time; it only defines
the proxy class + helpers. The ``__main__`` block starts a real listener.
"""
from __future__ import annotations

import copy
import errno
import json
import os
import re
import socket
import socketserver
import stat as stat_mod
import sys
import threading
import time
import urllib.parse

# Make sibling module importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harden_policy as hp  # noqa: E402

PROXY_VERSION = "d2b3b1-v1"

# ---------------------------------------------------------------------------
# Constants (design freeze §3, §5, §6)
# ---------------------------------------------------------------------------

DEFAULT_LISTEN_SOCKET = "/run/mp/docker.sock"
DEFAULT_UPSTREAM_SOCKET = "/var/run/docker.sock"
DEFAULT_SOCKET_MODE = 0o600  # owner-only; proxy runs as root
DEFAULT_SOCKET_DIR_MODE = 0o755
SOCKET_DIR = "/run/mp"

# Body size cap (design §3.3 D4.2). Matches the Skill common envelope limit.
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

# Upstream response size cap for non-streaming reads.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # 16 MiB

# Per-request deadlines (seconds). Fail-closed on expiry.
UPSTREAM_CONNECT_TIMEOUT = 5.0
UPSTREAM_RESPONSE_TIMEOUT = 30.0
HIJACK_HARD_TIMEOUT = 60.0

# Marker file (guarded_start.py contract; D2B-3A.1 §3.7 / B7)
PROXY_DEPLOYED_MARKER = "/etc/hiclab/proxy-deployed"

# ---------------------------------------------------------------------------
# Endpoint allowlist (design §3, §6.3). 13 SOURCE_PROVEN entries.
#
# Each entry: (regex, method_set, decision) where decision is one of
#   'readonly'    -> passthrough, no body inspection
#   'nameprefix'  -> inspect target container name (from path or query),
#                    require it to match the worker/manager regex
#   'transform'   -> POST /containers/create; run evaluate_deny + apply_hardening_v2
# The version prefix /v1.x is optional and stripped before matching.
# ---------------------------------------------------------------------------

# Methods per decision. NOTE: HEAD is NOT in the allowlist — only GET /_ping
# is allowed, and Docker's controller uses GET (docker.go:77). HEAD is denied.
_READONLY_METHODS = frozenset({"GET"})
_NAMEPREFIX_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})

# Container-name patterns. name_profile selects legacy (hiclaw-*) vs v1.2.2
# (agentteams-*). Default is v1.2.2.
_NAME_PROFILES = {
    "agentteams": (
        hp.AGENTTEAMS_WORKER_NAME_RE,
        hp.AGENTTEAMS_MANAGER_NAME_RE,
    ),
    "hiclaw": (
        hp.WORKER_NAME_RE,
        hp.MANAGER_NAME_RE,
    ),
}

# Auth-volume name pattern: {worker-name}-auth (docker.go:460 deleteAuthVolume)
_AUTH_VOLUME_RE = re.compile(r"^agentteams-worker-[a-z0-9-]+-auth$")
_AUTH_VOLUME_RE_LEGACY = re.compile(r"^hiclaw-worker-[a-z0-9-]+-auth$")

# Strip optional /v1.x prefix
_VERSION_PREFIX_RE = re.compile(r"^/v[\d.]+(?=/)")


def _strip_version(path):
    """Return path with an optional /v1.x prefix removed."""
    return _VERSION_PREFIX_RE.sub("", path, count=1) if path else path


# ---------------------------------------------------------------------------
# ProxyConfig (immutable deploy-owned config)
# ---------------------------------------------------------------------------


class ProxyConfig:
    """Deploy-owned, immutable proxy configuration.

    All allowlists are frozensets; mutation after construction is a bug.
    """

    def __init__(
        self,
        run_id,
        scope="prod",
        name_profile="agentteams",
        image_allowlist=(),
        bind_allowlist=(),
        network_allowlist=("agentteams-net", "hiclab-net"),
        tmpfs_sizes=None,
        storage_opt_supported=False,
        storage_opt_gib=None,
        log_max_size="10m",
        log_max_file=3,
        listen_socket=DEFAULT_LISTEN_SOCKET,
        upstream_socket=DEFAULT_UPSTREAM_SOCKET,
        manager_mode=False,
    ):
        self.run_id = str(run_id or "")
        if not self.run_id:
            raise ValueError("ProxyConfig.run_id must be non-empty")
        self.scope = str(scope or "prod")
        if self.scope not in ("prod", "test", "storageopt-probe"):
            raise ValueError("ProxyConfig.scope not in allowlist")
        if name_profile not in _NAME_PROFILES:
            raise ValueError("unknown name_profile: %r" % name_profile)
        self.name_profile = name_profile
        self.image_allowlist = frozenset(image_allowlist)
        if not self.image_allowlist:
            raise ValueError(
                "ProxyConfig.image_allowlist empty — fail-closed refusal "
                "to start without a fixed digest allowlist")
        self.bind_allowlist = frozenset(bind_allowlist)
        self.network_allowlist = frozenset(network_allowlist)
        self.tmpfs_sizes = dict(tmpfs_sizes or {})
        self.storage_opt_supported = bool(storage_opt_supported)
        self.storage_opt_gib = storage_opt_gib
        self.log_max_size = log_max_size
        self.log_max_file = int(log_max_file)
        self.listen_socket = listen_socket
        self.upstream_socket = upstream_socket
        self.manager_mode = bool(manager_mode)

    def hardening_config(self):
        """Return the dict apply_hardening_v2 expects."""
        return {
            "scope": self.scope,
            "run_id": self.run_id,
            "sizes": self.tmpfs_sizes,
            "storage_opt_supported": self.storage_opt_supported,
            "storage_opt_gib": self.storage_opt_gib,
            "log_max_size": self.log_max_size,
            "log_max_file": self.log_max_file,
        }

    def config_digest(self):
        """Stable short digest of the config (for marker binding)."""
        import hashlib
        h = hashlib.sha256()
        h.update(self.run_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.scope.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.name_profile.encode("utf-8"))
        for img in sorted(self.image_allowlist):
            h.update(img.encode("utf-8"))
            h.update(b"\n")
        for b in sorted(self.bind_allowlist):
            h.update(b.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Exec ID authorization map (design §6.3 + requirement 10)
# ---------------------------------------------------------------------------


class ExecRegistry:
    """Tracks exec IDs created by authorized containers.

    Only exec IDs created via an authorized POST /containers/{name}/exec (where
    {name} matched the worker/manager regex) may subsequently be started or
    inspected. Unknown / expired / cross-run exec IDs are denied. On proxy
    restart, the registry is empty -> fail-closed (all exec start/json denied
    until the controller re-creates them).
    """

    def __init__(self, ttl=300.0):
        self._ttl = float(ttl)
        self._ids = {}  # exec_id -> (created_at, container_name)
        self._lock = threading.Lock()

    def register(self, exec_id, container_name):
        if not exec_id or not container_name:
            return False
        with self._lock:
            self._ids[exec_id] = (time.monotonic(), container_name)
            self._gc_locked()
        return True

    def authorize(self, exec_id):
        """Return (ok, container_name) for an exec start/json request."""
        if not exec_id:
            return (False, None)
        with self._lock:
            self._gc_locked()
            entry = self._ids.get(exec_id)
            if entry is None:
                return (False, None)
            created, name = entry
            if time.monotonic() - created > self._ttl:
                del self._ids[exec_id]
                return (False, None)
            return (True, name)

    def _gc_locked(self):
        now = time.monotonic()
        stale = [eid for eid, (c, _n) in self._ids.items()
                 if now - c > self._ttl]
        for eid in stale:
            del self._ids[eid]

    def clear(self):
        with self._lock:
            self._ids.clear()


# ---------------------------------------------------------------------------
# Request classification (the heart of deny-by-default)
# ---------------------------------------------------------------------------


class Decision:
    """Result of classifying a request."""
    __slots__ = ("action", "reason", "body", "name", "exec_id",
                 "hijack", "stream")

    def __init__(self, action, reason="", body=None, name=None, exec_id=None,
                 hijack=False, stream=False):
        self.action = action  # 'allow' | 'deny' | 'transform'
        self.reason = reason
        self.body = body
        self.name = name
        self.exec_id = exec_id
        self.hijack = bool(hijack)
        self.stream = bool(stream)


def _parse_target_name(path, query):
    """Extract the container name from a /containers/{name}/... path or query.

    Special case: the literal path ``/containers/create`` is the create
    endpoint, NOT a container named "create" — its name comes from the
    ``?name=`` query parameter.
    """
    # /containers/create (exact) — name is in the query, not the path
    if path == "/containers/create":
        if isinstance(query, dict):
            n = query.get("name")
            if n:
                return urllib.parse.unquote(str(n))
        return None
    # /containers/{name}/...  (must have a trailing operation segment)
    m = re.match(r"^/containers/([^/]+)(?:/.*)?$", path)
    if m:
        return urllib.parse.unquote(m.group(1))
    # POST /containers/create?name=... (defensive; same as above)
    if isinstance(query, dict):
        n = query.get("name")
        if n:
            return urllib.parse.unquote(str(n))
    return None


def classify_request(method, raw_path, config, exec_registry, body=None,
                     query=None, target_header=None):
    """Classify a parsed Docker API request. Returns a Decision.

    deny-by-default: anything not explicitly allowed -> Decision('deny').
    """
    if not method or not raw_path:
        return Decision("deny", "missing method/path")

    method = method.upper()
    # Strip query string + parse
    parsed = urllib.parse.urlparse(raw_path)
    path = parsed.path or "/"
    qs = urllib.parse.parse_qs(parsed.query) if parsed.query else {}
    # Flatten single-value query dicts
    query = query if query is not None else {k: v[0] if v else ""
                                             for k, v in qs.items()}
    path_no_ver = _strip_version(path)

    # --- hijack / Upgrade detection (D4.1) ---
    # target_header carries the value of the HTTP "Upgrade" header if present.
    # Any non-empty Upgrade header means the client wants a hijacked connection
    # (Docker uses "Upgrade: tcp" for attach/exec). The proxy allows hijack
    # ONLY on /exec/{id}/start (see below); other Upgrade requests are denied
    # by the default-deny fallthrough.
    wants_upgrade = bool(target_header)

    # 1. GET /_ping (readonly, no name, no config needed) — docker.go:77
    #    Fast path: allow before touching config so even a minimal smoke test
    #    can reach _ping without a full ProxyConfig.
    if method in _READONLY_METHODS and path_no_ver == "/_ping":
        return Decision("allow", "_ping", stream=True)

    # All other branches need the config (name profile + allowlists).
    if config is None:
        return Decision("deny", "config required for this endpoint")
    worker_re, manager_re = _NAME_PROFILES[config.name_profile]

    # 2. POST /containers/create?name= (transform) — docker.go:396
    if method == "POST" and path_no_ver == "/containers/create":
        name = _parse_target_name(path_no_ver, query) or ""
        name = name.lstrip("/")
        kind = None
        if worker_re.match(name):
            kind = "worker"
        elif manager_re.match(name):
            kind = "manager"
        if kind is None:
            return Decision("deny", "create name not worker/manager: %r"
                            % name[:32])
        # name must be ASCII, no traversal
        if not _is_safe_name(name):
            return Decision("deny", "unsafe name chars: %r" % name[:32])
        # D2B-3C: reject worker names whose agent suffix is not a known role.
        # derive_agent_strict returns None for unknown agents (e.g.
        # agentteams-worker-evil) — deny at classify, never create a container
        # with an unparseable identity.
        if hp.derive_agent_strict(name) is None:
            return Decision("deny",
                            "unknown agent role in name: %r" % name[:32])
        return Decision("transform", "create %s" % kind, body=body,
                        name=name)

    # 3. POST /containers/{name}/exec — docker.go:320 (creates exec; must
    #    remember the exec_id when the upstream returns it)
    m = re.match(r"^/containers/([^/]+)/exec$", path_no_ver)
    if method == "POST" and m:
        name = urllib.parse.unquote(m.group(1))
        if not _name_authorized(name, worker_re, manager_re):
            return Decision("deny", "exec target not authorized: %s"
                            % name[:32])
        # This request creates an exec; response body has the exec_id which
        # the caller (ProxyHandler) will register. Mark hijack-aware.
        return Decision("allow", "exec create", name=name,
                        hijack=False, stream=True)

    # 4. POST /exec/{id}/start — docker.go:345 (hijack upgrade)
    m = re.match(r"^/exec/([^/]+)/start$", path_no_ver)
    if method == "POST" and m:
        exec_id = urllib.parse.unquote(m.group(1))
        ok, _name = exec_registry.authorize(exec_id)
        if not ok:
            return Decision("deny", "exec id not authorized: %s" % exec_id[:16])
        if not wants_upgrade:
            # /exec/{id}/start without Upgrade is unusual but not forbidden;
            # still allow (the upstream will reject if it requires hijack).
            return Decision("allow", "exec start (no upgrade)", exec_id=exec_id,
                            stream=True)
        return Decision("allow", "exec start (hijack)", exec_id=exec_id,
                        hijack=True, stream=True)

    # 5. GET /exec/{id}/json — docker.go:362
    m = re.match(r"^/exec/([^/]+)/json$", path_no_ver)
    if method == "GET" and m:
        exec_id = urllib.parse.unquote(m.group(1))
        ok, _name = exec_registry.authorize(exec_id)
        if not ok:
            return Decision("deny", "exec id not authorized: %s" % exec_id[:16])
        return Decision("allow", "exec json", exec_id=exec_id)

    # 6-9. /containers/{name}/{start|stop|json|archive} — the only 4 sub-ops
    #      the controller calls (docker.go:617/490/516/266). logs/stats/changes/
    #      wait are NOT in the 13-endpoint allowlist (design freeze §3) -> deny.
    m = re.match(r"^/containers/([^/]+)/(start|stop|json|archive)$", path_no_ver)
    if m:
        name = urllib.parse.unquote(m.group(1))
        op = m.group(2)
        allowed_methods = {
            "start": {"POST"},
            "stop": {"POST"},
            "json": {"GET"},
            "archive": {"PUT"},   # docker.go:266 (auth token projection)
        }
        if method not in allowed_methods.get(op, set()):
            return Decision("deny", "method %s not allowed for /containers/{name}/%s"
                            % (method, op))

        # STRICT QUERY VALIDATION (design freeze §6 / req 6)
        # stop: only ?t=10 (docker.go:490 hardcodes t=10)
        if op == "stop":
            if query != {"t": "10"}:
                return Decision("deny", "stop requires exactly ?t=10, got %r"
                                % query)
        # archive: path must be the auth-token dir (B11 fix; see _ARCHIVE_PATH_OK)
        elif op == "archive":
            qpath = query.get("path", "")
            if not _archive_path_allowed(qpath):
                return Decision("deny",
                                "archive path not in auth-token allowlist: %r"
                                % qpath[:48])
        elif op in ("start", "json"):
            # these take no query params in the controller's usage
            if query:
                return Decision("deny", "%s accepts no query, got %r"
                                % (op, query))

        if not _name_authorized(name, worker_re, manager_re):
            return Decision("deny", "%s target not authorized: %s"
                            % (op, name[:32]))
        return Decision("allow", "containers/%s" % op, name=name)

    # 10. DELETE /containers/{name}?force=true — docker.go:441 (force=true only)
    m = re.match(r"^/containers/([^/]+)/?$", path_no_ver)
    if method == "DELETE" and m:
        name = urllib.parse.unquote(m.group(1))
        # STRICT: only ?force=true (docker.go:441 uses force=true). Any other
        # query (force=false, force=1, extra params, duplicate, empty) -> deny.
        if query != {"force": "true"}:
            return Decision("deny", "delete requires exactly ?force=true, got %r"
                            % query)
        if not _name_authorized(name, worker_re, manager_re):
            return Decision("deny", "delete target not authorized: %s"
                            % name[:32])
        return Decision("allow", "delete container", name=name)

    # 12. DELETE /volumes/{authVolumeName} — docker.go:460
    m = re.match(r"^/volumes/([^/]+)/?$", path_no_ver)
    if method == "DELETE" and m:
        vol = urllib.parse.unquote(m.group(1))
        vol_re = (_AUTH_VOLUME_RE_LEGACY if config.name_profile == "hiclaw"
                  else _AUTH_VOLUME_RE)
        if not vol_re.match(vol):
            return Decision("deny", "auth volume name not authorized: %s"
                            % vol[:32])
        return Decision("allow", "delete auth volume")

    # 13. GET /images/{image}/json — docker.go:568
    m = re.match(r"^/images/(.+)/json$", path_no_ver)
    if method == "GET" and m:
        img = urllib.parse.unquote(m.group(1))
        # Only allowlisted images may be inspected
        digest = img.rsplit("@", 1)[-1] if "@" in img else img
        if not (digest.startswith("sha256:")
                and digest in config.image_allowlist):
            return Decision("deny", "image not in allowlist: %s" % img[:32])
        return Decision("allow", "image inspect")

    # 14. POST /images/create?fromImage= — docker.go:585
    if method == "POST" and path_no_ver == "/images/create":
        from_img = query.get("fromImage", "") if isinstance(query, dict) else ""
        from_img = urllib.parse.unquote(str(from_img))
        digest = from_img.rsplit("@", 1)[-1] if "@" in from_img else from_img
        if not (digest.startswith("sha256:")
                and digest in config.image_allowlist):
            return Decision("deny", "pull image not in allowlist: %s"
                            % from_img[:32])
        return Decision("allow", "image pull", stream=True)

    # --- Everything else: deny (D2/D3/D4 / fail-closed) ---
    return Decision("deny", "endpoint not in allowlist: %s %s"
                    % (method, path_no_ver[:48]))


def _is_safe_name(name):
    """Container name must be ASCII, no path traversal, no null bytes."""
    if not name:
        return False
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return False
    if "/" in name or ".." in name or "\x00" in name:
        return False
    return True


def _name_authorized(name, worker_re, manager_re):
    """Return True if name matches worker or manager regex."""
    if not _is_safe_name(name):
        return False
    return bool(worker_re.match(name) or manager_re.match(name))


# ---------------------------------------------------------------------------
# B11 fix: auth-token archive path allowlist.
#
# SOURCE-PROVEN from AgentTeams v1.2.2 (commit 849182a):
#   - DefaultAuthTokenFile = "/var/run/secrets/agentteams/token"
#     (agentteams-controller/internal/backend/interface.go:47)
#   - PUT /containers/{name}/archive?path=<dir> uploads to
#     path.Dir(DefaultAuthTokenFile) = "/var/run/secrets/agentteams"
#     (docker.go:266)
#   - token rotation uses DefaultAuthTokenFile + ".next" then `mv -f`
#     (docker.go:292,299), so the archive upload also targets the same dir.
#
# The proxy allows ONLY this exact directory (and the canonical token file
# path). /etc, /, .., absolute-path bypasses, encoding tricks, and any other
# path are DENIED at classify time — never forwarded to dockerd.
# ---------------------------------------------------------------------------

AUTH_TOKEN_DIR = "/var/run/secrets/agentteams"
AUTH_TOKEN_FILE = AUTH_TOKEN_DIR + "/token"
AUTH_TOKEN_FILE_NEXT = AUTH_TOKEN_FILE + ".next"

# Exact set of archive ?path= values the controller ever sends. Anything else
# is denied. Both the directory (for the initial tar upload) and the explicit
# file paths (defensive) are accepted.
_ARCHIVE_PATH_ALLOWLIST = frozenset({
    AUTH_TOKEN_DIR,
    AUTH_TOKEN_FILE,
    AUTH_TOKEN_FILE_NEXT,
})


def _archive_path_allowed(raw_path):
    """Return True iff ``raw_path`` is an allowed archive target (B11).

    Performs strict validation:
      - URL-decode once (the controller sends url.QueryEscape'd paths)
      - reject if decoding changes the path structurally (double-encoding
        bypass like %252e%252e -> %2e%2e -> ..)
      - normalize and compare against the exact allowlist
      - reject empty, /, /etc, .., backslashes, null bytes
    """
    if not raw_path or not isinstance(raw_path, str):
        return False
    # Detect double-encoding: if the raw value contains a '%' that itself is
    # encoded (i.e. '%25' present), reject — the controller never sends that.
    if "%25" in raw_path:
        return False
    # Single decode
    decoded = urllib.parse.unquote(raw_path)
    # Reject if decode introduced traversal or null
    if "\x00" in decoded or ".." in decoded:
        return False
    # Normalize: must be an exact allowlist member (no trailing slash variants
    # except the dir itself). Use os.path.normpath but then require exact match
    # against the allowlist (normpath("/var/run/secrets/agentteams/") == dir).
    import posixpath
    norm = posixpath.normpath(decoded)
    if norm in _ARCHIVE_PATH_ALLOWLIST:
        return True
    return False


# ---------------------------------------------------------------------------
# Unix socket listener + lifecycle
# ---------------------------------------------------------------------------


def _safe_remove(path):
    """Remove a path, ignoring 'not found' but surfacing other errors."""
    try:
        os.unlink(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def _is_symlink_or_unsafe(path):
    """Return True if path is a symlink or its parent is not a trusted dir."""
    try:
        st = os.lstat(path)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return False  # doesn't exist yet — safe to create
        return True  # other error — treat as unsafe
    if stat_mod.S_ISLNK(st.st_mode):
        return True
    return False


def prepare_socket_dir(listen_socket):
    """Ensure the socket's parent dir exists with safe permissions.

    Refuses to create the socket if the path is a symlink (design §12:
    'do not follow untrusted symlink'). Idempotent.
    """
    parent = os.path.dirname(os.path.abspath(listen_socket))
    if _is_symlink_or_unsafe(listen_socket):
        raise RuntimeError(
            "refusing to bind: %s is a symlink or unsafe" % listen_socket)
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=DEFAULT_SOCKET_DIR_MODE)
    else:
        # Verify parent is not a symlink chain we don't control
        try:
            real_parent = os.path.realpath(parent)
            if real_parent != parent:
                # Parent resolves through symlinks; allow only SOCKET_DIR
                if real_parent != os.path.realpath(SOCKET_DIR):
                    raise RuntimeError(
                        "socket parent %s resolves through symlinks" % parent)
        except OSError:
            raise RuntimeError("cannot resolve socket parent: %s" % parent)


def bind_listening_socket(listen_socket):
    """Bind the Unix listening socket. Cleans up any stale socket first.

    Returns the bound socket object (caller accepts on it). Raises on any
    failure (caller must not leave a half-bound socket).
    """
    prepare_socket_dir(listen_socket)
    # Remove stale socket from a previous crash (do NOT follow symlinks)
    if os.path.exists(listen_socket):
        if _is_symlink_or_unsafe(listen_socket):
            raise RuntimeError(
                "refusing to remove: %s is a symlink" % listen_socket)
        _safe_remove(listen_socket)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(listen_socket)
        os.chmod(listen_socket, DEFAULT_SOCKET_MODE)
        sock.listen(64)
    except Exception:
        sock.close()
        # Best-effort cleanup of a half-bound socket
        try:
            _safe_remove(listen_socket)
        except OSError:
            pass
        raise
    return sock


# ---------------------------------------------------------------------------
# Marker lifecycle (guarded_start.py contract; D2B-3A.1 §3.7 / B7)
# ---------------------------------------------------------------------------

MARKER_PREFIX = b"hiclab-proxy:deployed:v1\n"


def marker_content(pid, config_digest):
    """Build the versioned marker body binding proxy PID + config digest.

    Format (extend the existing v1 line with pid= and digest= on subsequent
    lines; guarded_start.validate_marker is extended to accept this v1.1
    format while still accepting the bare v1 line for backward compat).
    """
    return (MARKER_PREFIX
            + ("pid=%d\n" % int(pid)).encode("ascii")
            + ("digest=%s\n" % str(config_digest)).encode("ascii"))


def write_marker(pid, config_digest, path=PROXY_DEPLOYED_MARKER,
                 write_fn=None, chmod_fn=None):
    """Atomically write the proxy marker. Caller must be root.

    Returns True on success, False on any failure (fail-closed: never leaves
    a partial marker). Uses the injected write_fn/chmod_fn for testability.
    """
    write_fn = write_fn or _atomic_write_bytes
    chmod_fn = chmod_fn or _chmod_0600
    content = marker_content(pid, config_digest)
    try:
        write_fn(path, content)
        chmod_fn(path)
    except Exception:
        # Best-effort cleanup
        try:
            os.unlink(path)
        except OSError:
            pass
        return False
    return True


def remove_marker(path=PROXY_DEPLOYED_MARKER, unlink_fn=None):
    """Remove the marker (idempotent). Used on proxy shutdown."""
    unlink_fn = unlink_fn or os.unlink
    try:
        unlink_fn(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def _atomic_write_bytes(path, data):
    """Write bytes to path atomically (temp + rename)."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _chmod_0600(path):
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# ProxyServer: glue
# ---------------------------------------------------------------------------


class ProxyServer:
    """Owns the config, exec registry, and lifecycle (socket + marker).

    The actual per-connection handling is done by ProxyRequestHandler (below),
    which reads this server object for config + exec_registry.
    """

    def __init__(self, config, exec_registry=None, pid=None,
                 write_marker_fn=None, remove_marker_fn=None):
        self.config = config
        self.exec_registry = exec_registry or ExecRegistry()
        self.pid = int(pid if pid is not None else os.getpid())
        self._write_marker_fn = write_marker_fn or write_marker
        self._remove_marker_fn = remove_marker_fn or remove_marker
        self._marker_written = False
        self._sock = None
        self._shutdown = False
        # Audit log (in-process; real deployment writes INSERT-only to DB)
        self.audit = []

    def startup_self_check(self):
        """Verify config is complete and upstream socket is reachable.

        Returns (ok, reason). Called BEFORE writing the marker. On any failure
        the proxy must exit non-zero and NOT write the marker.
        """
        if not self.config.image_allowlist:
            return (False, "image_allowlist empty")
        # Verify upstream socket exists and is connectable
        try:
            test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            test.settimeout(UPSTREAM_CONNECT_TIMEOUT)
            test.connect(self.config.upstream_socket)
            test.close()
        except OSError as e:
            return (False, "upstream %s unreachable: %s"
                    % (self.config.upstream_socket, e))
        return (True, "self-check ok")

    def arm_marker(self):
        """Write the marker (called after self_check passes + listener bound)."""
        digest = self.config.config_digest()
        ok = self._write_marker_fn(self.pid, digest)
        if ok:
            self._marker_written = True
        return ok

    def disarm_marker(self):
        """Remove the marker (called on shutdown / fatal error)."""
        if self._marker_written:
            try:
                self._remove_marker_fn()
            finally:
                self._marker_written = False

    def cleanup_socket(self):
        """Remove the listening socket (shutdown)."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        try:
            _safe_remove(self.config.listen_socket)
        except OSError:
            pass

    def shutdown(self):
        """Disarm marker + cleanup socket. Idempotent."""
        self._shutdown = True
        self.disarm_marker()
        self.cleanup_socket()


def main(argv=None):
    """Real-process entry point. Parses env, binds socket, serves forever.

    This is NOT exercised by the unit tests (they use the in-process
    ProxyServer + FakeUpstreamDaemon). It exists for real deployment.
    """
    argv = argv if argv is not None else sys.argv[1:]
    run_id = os.environ.get("MERGEPILOT_RUN_ID", "")
    scope = os.environ.get("MERGEPILOT_SCOPE", "prod")
    name_profile = os.environ.get("MERGEPILOT_NAME_PROFILE", "agentteams")
    images_raw = os.environ.get("MERGEPILOT_IMAGE_ALLOWLIST", "")
    if not run_id or not images_raw:
        sys.stderr.write(
            "MERGEPILOT_RUN_ID and MERGEPILOT_IMAGE_ALLOWLIST required\n")
        return 2
    image_allowlist = frozenset(
        s.strip() for s in images_raw.split(",") if s.strip())
    config = ProxyConfig(
        run_id=run_id, scope=scope, name_profile=name_profile,
        image_allowlist=image_allowlist,
    )
    server_obj = ProxyServer(config)
    ok, reason = server_obj.startup_self_check()
    if not ok:
        sys.stderr.write("self-check FAILED: %s\n" % reason)
        return 2
    try:
        server_obj._sock = bind_listening_socket(config.listen_socket)
    except Exception as e:
        sys.stderr.write("bind failed: %s\n" % e)
        return 2
    if not server_obj.arm_marker():
        sys.stderr.write("marker write failed; aborting\n")
        server_obj.cleanup_socket()
        return 2
    sys.stderr.write("proxy ready: %s (pid=%d)\n"
                     % (config.listen_socket, server_obj.pid))
    # Real accept loop: each connection handed to a daemon thread that runs
    # the production transport handler (proxy_transport.handle_connection).
    # The handler does full HTTP parse + classify + authoritative inspect +
    # forward + relay + hijack, all fail-closed.
    import threading
    import proxy_transport as _pt
    threads = []
    try:
        while not server_obj._shutdown:
            try:
                conn, _addr = server_obj._sock.accept()
            except OSError:
                break
            conn.settimeout(None)  # handler uses select()+deadlines
            t = threading.Thread(
                target=_pt.handle_connection,
                args=(conn, config.upstream_socket, config,
                      server_obj.exec_registry),
                daemon=True)
            t.start()
            threads.append(t)
            # reap finished threads to avoid unbounded list
            if len(threads) > 64:
                threads = [x for x in threads if x.is_alive()]
    except KeyboardInterrupt:
        pass
    finally:
        server_obj.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

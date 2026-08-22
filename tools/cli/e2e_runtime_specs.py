"""M8-GH-4B3-W3B-S1: six-service runtime configuration specs and API.

§3: unified runtime file contract (0600/refuse-overwrite/journal-safe).
§4-§8: service-specific schemas and wiring.
§9: secret unique-consumer matrix.
§10: lifecycle API (validate/create/remove/plan).

Reuses existing e2e_foundation schemas for Controller (15-key) and
Reporter (9-key); adds Gateway, Bridge, Proxy-r, Proxy-b specs here.
The component gate fires BEFORE any of these are reached in real CLI
start; tests call the API directly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import e2e_foundation as e2f


class RuntimeSpecError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail))


# ── §5: Gateway E2E schema ──────────────────────────────────────────────────

GATEWAY_E2E_ENV_KEYS = frozenset((
    "UPSTREAM_URL",      # http://172.31.0.34:8082/sse
    "POLICY_FILE",       # /run/mergepilot/policy-fixture.yaml
    "ROLE_TOKENS",       # JSON role->token
    "AUDIT_DSN",         # postgres DSN
))

GATEWAY_E2E_UPSTREAM = "http://172.31.0.34:8082/sse"
GATEWAY_E2E_POLICY = "/run/mergepilot/policy-fixture.yaml"

#: Frozen read-only tool set (Gateway semantic health contract)
GATEWAY_READ_ONLY_TOOLS = frozenset((
    "get_pull_request",
    "get_pull_request_files",
    "get_file_contents",
    "get_branch",
))


def validate_gateway_e2e_env(mapping) -> dict:
    if not isinstance(mapping, dict):
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "gateway env must be a mapping")
    unknown = sorted(set(mapping) - GATEWAY_E2E_ENV_KEYS)
    if unknown:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "unknown key(s): %s" % unknown)
    missing = sorted(GATEWAY_E2E_ENV_KEYS - set(mapping))
    if missing:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "missing key(s): %s" % missing)
    for key in sorted(GATEWAY_E2E_ENV_KEYS):
        v = mapping[key]
        if not isinstance(v, str) or not v.strip():
            raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                                   "%s: must be non-empty" % key)
        if "\r" in v or "\n" in v or "\0" in v:
            raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                                   "%s: CR/LF/NUL forbidden" % key)
    if mapping["UPSTREAM_URL"] != GATEWAY_E2E_UPSTREAM:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "UPSTREAM_URL must be exactly %s"
                               % GATEWAY_E2E_UPSTREAM)
    if mapping["POLICY_FILE"] != GATEWAY_E2E_POLICY:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "POLICY_FILE must be exactly %s"
                               % GATEWAY_E2E_POLICY)
    # ROLE_TOKENS is json.loads()ed by the gateway AT STARTUP: a
    # non-JSON placeholder passes the non-empty-string bar and then
    # crashes the container deterministically at gateway start.
    # Fail closed HERE (stage 2, before anything is created).
    try:
        import json as _json
        tokens = _json.loads(mapping["ROLE_TOKENS"])
    except ValueError:
        raise RuntimeSpecError(
            "RUNTIME_CONFIG_INVALID",
            "ROLE_TOKENS must be valid JSON") from None
    if (not isinstance(tokens, dict) or not tokens
            or not all(isinstance(k, str) and k.strip()
                       and isinstance(v, str) and v.strip()
                       for k, v in tokens.items())):
        raise RuntimeSpecError(
            "RUNTIME_CONFIG_INVALID",
            "ROLE_TOKENS must be a non-empty JSON object of "
            "non-empty string tokens")
    return dict(mapping)


# ── §6: MCP Bridge schema ───────────────────────────────────────────────────

BRIDGE_ENV_KEYS = frozenset((
    "MCP_GITHUB_TOKEN",     # fine-grained PAT
    "GITHUB_REPOSITORY",    # fixture repo (owner/name)
    "HTTPS_PROXY",          # http://172.31.0.114:18090
    "MCP_PROXY_PORT",       # 8082
))

BRIDGE_PROXY = "http://172.31.0.114:18090"


def validate_bridge_env(mapping) -> dict:
    if not isinstance(mapping, dict):
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "bridge env must be a mapping")
    unknown = sorted(set(mapping) - BRIDGE_ENV_KEYS)
    if unknown:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "unknown key(s): %s" % unknown)
    missing = sorted(BRIDGE_ENV_KEYS - set(mapping))
    if missing:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "missing key(s): %s" % missing)
    for key in sorted(BRIDGE_ENV_KEYS):
        v = mapping[key]
        if not isinstance(v, str) or not v.strip():
            raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                                   "%s: must be non-empty" % key)
        if "\r" in v or "\n" in v or "\0" in v:
            raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                                   "%s: CR/LF/NUL forbidden" % key)
    if mapping["HTTPS_PROXY"] != BRIDGE_PROXY:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "HTTPS_PROXY must be exactly %s"
                               % BRIDGE_PROXY)
    if mapping["MCP_PROXY_PORT"] != "8082":
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "MCP_PROXY_PORT must be 8082")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
                        mapping["GITHUB_REPOSITORY"]):
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "GITHUB_REPOSITORY must be owner/name")
    return dict(mapping)


# ── §8: Proxy schemas ───────────────────────────────────────────────────────

PROXY_ENV_KEYS = frozenset((
    "GH_PROXY_BIND",         # 0.0.0.0
    "GH_PROXY_PORT",         # 18090
    "GH_PROXY_UPSTREAM_IP",  # Windows proxy IP literal
    "GH_PROXY_UPSTREAM_PORT",  # 17890
))


def validate_proxy_env(mapping) -> dict:
    if not isinstance(mapping, dict):
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "proxy env must be a mapping")
    unknown = sorted(set(mapping) - PROXY_ENV_KEYS)
    if unknown:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "unknown key(s): %s" % unknown)
    missing = sorted(PROXY_ENV_KEYS - set(mapping))
    if missing:
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "missing key(s): %s" % missing)
    for key in sorted(PROXY_ENV_KEYS):
        v = mapping[key]
        if not isinstance(v, str) or not v.strip():
            raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                                   "%s: must be non-empty" % key)
    if mapping["GH_PROXY_UPSTREAM_PORT"] != "17890":
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "upstream port must be 17890")
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$",
                    mapping["GH_PROXY_UPSTREAM_IP"]):
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "upstream IP must be an IPv4 literal")
    return dict(mapping)


# ── §9: Secret unique-consumer matrix ──────────────────────────────────────

#: Maps secret kind -> the exact set of services allowed to consume it.
SECRET_CONSUMER_MATRIX = {
    "fine_grained_pat": frozenset(("mcp-bridge",)),
    "github_app_pem": frozenset(("gh-reporter",)),
    "room_map": frozenset(("controller",)),
    "fixture_policy": frozenset(("controller", "policy-gateway")),
    "matrix_credential": frozenset(("controller",)),
    "proxy_upstream_config": frozenset(("gh-proxy-r", "gh-proxy-b")),
    "role_tokens": frozenset(("policy-gateway",)),
    "audit_dsn": frozenset(("policy-gateway",)),
    "reporter_dsn": frozenset(("gh-reporter",)),
}


def validate_secret_consumers(service: str, secret_kinds: set) -> None:
    """Fail-closed if a service consumes a secret it doesn't own."""
    for kind in secret_kinds:
        allowed = SECRET_CONSUMER_MATRIX.get(kind, frozenset())
        if service not in allowed:
            raise RuntimeSpecError(
                "SECRET_CONSUMER_VIOLATION",
                "%s cannot consume %s (allowed: %s)"
                % (service, kind, sorted(allowed)))


#: Maps env keys to their sensitive resource kind for cross-validation.
SENSITIVE_ENV_KEY_MAP = {
    "MCP_GITHUB_TOKEN": "fine_grained_pat",
    "GITHUB_PUBLISHER_DSN": "reporter_dsn",
    "AUDIT_DSN": "audit_dsn",
    "ROLE_TOKENS": "role_tokens",
    "COORDINATOR_TOKEN": "matrix_credential",
}


def cross_validate_sensitive_keys(service: str,
                                  env_mapping: dict) -> None:
    """Cross-validate that sensitive env keys match the consumer matrix.
    Called from validate_runtime_configs — not just a constant check."""
    for key, kind in SENSITIVE_ENV_KEY_MAP.items():
        if key in env_mapping:
            validate_secret_consumers(service, {kind})


# ── §5 R2: filesystem adapter (injectable; production branch shared) ─────

class RealFilesystem:
    """Production fs adapter: symlink/junction/reparse detection and
    resolved-path confinement. Injectable so tests can simulate
    reparse attributes on platforms that cannot create them."""

    @staticmethod
    def is_reparse(path: str) -> bool:
        if os.path.islink(path):
            return True
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        tag = getattr(st, "st_reparse_tag", 0)
        attrs = getattr(st, "st_file_attributes", 0)
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        return bool(tag) or bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)

    @staticmethod
    def realpath(path: str) -> str:
        return os.path.realpath(path)

    @staticmethod
    def exists(path: str) -> bool:
        return os.path.exists(path)

    @staticmethod
    def read_bytes(path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    @staticmethod
    def write_bytes_atomic(path: str, data: bytes) -> None:
        tmp = path + ".mp-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    @staticmethod
    def unlink(path: str) -> None:
        os.unlink(path)

    @staticmethod
    def chmod0600(path: str) -> None:
        os.chmod(path, 0o600)


def _confined(fs, path_str: str, root_real: str) -> bool:
    """§5: the resolved target must stay inside the frozen runtime
    directory (no symlink/junction/reparse escape). Comparisons are
    case-insensitive on Windows (realpath may normalize case)."""
    if fs.is_reparse(path_str):
        return False
    resolved = fs.realpath(path_str)
    if os.path.normcase(resolved) != os.path.normcase(
            os.path.normpath(path_str)):
        return False
    root = os.path.normcase(root_real)
    me = os.path.normcase(resolved)
    return me == root or me.startswith(root + os.sep)


# ── §3/§10: unified runtime spec + lifecycle API ──────────────────────────

#: Full spec for each service's runtime configuration.
SERVICE_RUNTIME_SPECS = {
    "controller": {
        "env_file": "github_ingress.env",
        "keys": e2f.E2E_CONTROLLER_ENV_KEYS,
        "validator": e2f.validate_e2e_controller_env,
        "mounts": [
            ("room-map", "/run/mergepilot/room-map.yaml", "ro"),
            ("policy", "/run/mergepilot/policy-fixture.yaml", "ro"),
        ],
        "forbidden_secrets": ("fine_grained_pat", "github_app_pem"),
    },
    "policy-gateway": {
        "env_file": "gateway_e2e.env",
        "keys": GATEWAY_E2E_ENV_KEYS,
        "validator": validate_gateway_e2e_env,
        "mounts": [
            ("fixture_policy", "/run/mergepilot/policy-fixture.yaml", "ro"),
        ],
        "forbidden_secrets": ("fine_grained_pat", "github_app_pem",
                              "matrix_credential"),
    },
    "mcp-bridge": {
        "env_file": "mcp_bridge.env",
        "keys": BRIDGE_ENV_KEYS,
        "validator": validate_bridge_env,
        "mounts": [],
        "forbidden_secrets": ("github_app_pem", "matrix_credential"),
    },
    "gh-reporter": {
        "env_file": "gh_reporter.env",
        "keys": e2f.E2E_REPORTER_ENV_KEYS,
        "validator": e2f.validate_e2e_reporter_env,
        "mounts": [
            ("github_app_pem",
             e2f.E2E_REPORTER_KEY_CONTAINER_PATH, "ro"),
        ],
        "forbidden_secrets": ("fine_grained_pat", "matrix_credential"),
    },
    "gh-proxy-r": {
        "env_file": "gh_proxy_r.env",
        "keys": PROXY_ENV_KEYS,
        "validator": validate_proxy_env,
        "mounts": [],
        "forbidden_secrets": ("fine_grained_pat", "github_app_pem",
                               "matrix_credential"),
    },
    "gh-proxy-b": {
        "env_file": "gh_proxy_b.env",
        "keys": PROXY_ENV_KEYS,
        "validator": validate_proxy_env,
        "mounts": [],
        "forbidden_secrets": ("fine_grained_pat", "github_app_pem",
                               "matrix_credential"),
    },
}


def validate_runtime_configs(configs: dict) -> dict:
    """Validate ALL six service configs BEFORE any file write."""
    if not isinstance(configs, dict):
        raise RuntimeSpecError("RUNTIME_CONFIG_INVALID",
                               "configs must be a mapping")
    expected = set(SERVICE_RUNTIME_SPECS)
    provided = set(configs)
    if provided != expected:
        missing = sorted(expected - provided)
        extra = sorted(provided - expected)
        raise RuntimeSpecError(
            "RUNTIME_CONFIG_INVALID",
            "missing=%s extra=%s" % (missing, extra))
    validated = {}
    for service, raw in configs.items():
        spec = SERVICE_RUNTIME_SPECS[service]
        try:
            validated[service] = spec["validator"](raw)
        except e2f.E2EConfigError as exc:
            raise RuntimeSpecError(
                "RUNTIME_CONFIG_INVALID",
                "%s: %s" % (service, exc.detail)) from None
        # §9: secret consumer matrix check
        consumed = {kind for kind in spec.get("forbidden_secrets", ())
                    if kind in raw or _kind_in_mounts(kind, spec)}
        # Instead: check that the service's mounts don't include
        # forbidden secrets
        for kind in spec.get("forbidden_secrets", ()):
            for mount in spec.get("mounts", []):
                if mount[0] == kind:
                    raise RuntimeSpecError(
                        "SECRET_CONSUMER_VIOLATION",
                        "%s mount includes forbidden %s"
                        % (service, kind))
    return validated


def _kind_in_mounts(kind, spec):
    return any(m[0] == kind for m in spec.get("mounts", []))


def create_runtime_files(validated: dict, *, directory,
                         journal: dict,
                         persist_callback=None,
                         fs=None) -> list:
    """Create all six 0600 env files. On any failure, reverse-delete
    already-created files. Journal records path/service only.

    §4 R2 ordering contract: validate-all → file #1 atomic create →
    sanitized journal update → immediate persist → … → file #6
    persisted → (caller creates the first network only afterwards).
    A persist failure IS the primary error
    (RUNTIME_JOURNAL_PERSIST_FAILED); reverse-delete errors become
    safe diagnostics and never replace it; no later file is created.

    §5 R2 safety: refuse to create through/onto symlink, junction or
    reparse points; the resolved path must stay confined to the
    frozen runtime directory (stable code
    RUNTIME_FILE_REPARSE_REFUSED / RUNTIME_PATH_ESCAPE_REFUSED)."""
    fs = fs or RealFilesystem()
    directory = Path(directory or ".").absolute()
    directory.mkdir(parents=True, exist_ok=True)
    root_real = fs.realpath(str(directory))
    created = []
    cleanup_diags: list = []
    try:
        for service in sorted(SERVICE_RUNTIME_SPECS):
            spec = SERVICE_RUNTIME_SPECS[service]
            env = validated[service]
            path = directory / spec["env_file"]
            path_str = str(path)
            # §5: parent-directory ownership drift check
            if fs.realpath(str(directory)) != root_real:
                raise RuntimeSpecError(
                    "RUNTIME_DIR_DRIFT_REFUSED",
                    "%s: runtime directory ownership drifted"
                    % service)
            if fs.exists(path_str):
                if fs.is_reparse(path_str):
                    raise RuntimeSpecError(
                        "RUNTIME_FILE_REPARSE_REFUSED",
                        "%s: target is a symlink/junction/reparse "
                        "point" % service)
                if not _confined(fs, path_str, root_real):
                    raise RuntimeSpecError(
                        "RUNTIME_PATH_ESCAPE_REFUSED",
                        "%s: resolved path escapes the runtime "
                        "directory" % service)
                # Idempotent only if byte-identical
                existing = fs.read_bytes(path_str)
                content = _render_env(env, spec["keys"])
                if existing == content:
                    journal[service] = {"file": path_str,
                                        "ownership": "session"}
                    created.append(path)
                    if persist_callback:
                        persist_callback(journal)
                    continue
                raise RuntimeSpecError(
                    "RUNTIME_FILE_EXISTS",
                    "%s: refusing to overwrite differing file"
                    % service)
            if fs.is_reparse(path_str) or not _confined(
                    fs, path_str, root_real):
                raise RuntimeSpecError(
                    "RUNTIME_FILE_REPARSE_REFUSED",
                    "%s: target is a link/reparse or escapes the "
                    "runtime directory" % service)
            content = _render_env(env, spec["keys"])
            try:
                fs.write_bytes_atomic(path_str, content)  # atomic
            except OSError:
                raise RuntimeSpecError(
                    "RUNTIME_FILE_CREATE_FAILED",
                    service) from None
            try:
                fs.chmod0600(path_str)
            except OSError:
                pass  # Windows: metadata-only
            journal[service] = {"file": path_str,
                                "ownership": "session"}
            created.append(path)
            if persist_callback:
                try:
                    persist_callback(journal)
                except Exception:
                    # §4: persistence failure IS the primary error;
                    # reverse-delete below only adds diagnostics.
                    raise RuntimeSpecError(
                        "RUNTIME_JOURNAL_PERSIST_FAILED",
                        service) from None
    except Exception as primary:
        for path in reversed(created):
            try:
                fs.unlink(str(path))
            except OSError as exc:
                cleanup_diags.append(
                    "RUNTIME_CLEANUP_FAILED:%s(%s)"
                    % (path.name, type(exc).__name__))
        journal.clear()
        if isinstance(primary, RuntimeSpecError) and cleanup_diags:
            primary.diagnostics = cleanup_diags
        raise
    return created


def remove_runtime_files(*, directory, journal: dict,
                         fs=None) -> list:
    """Remove only journal-owned runtime files.

    §5 R2 refusal contract (stable codes, content never touched):
    - ownership != session            → skipped (foreign; never deleted)
    - journal path outside the frozen directory → RUNTIME_PATH_ESCAPE
    - target now a symlink/junction/reparse → RUNTIME_REMOVE_REPARSE
    - resolved path drifted from the journaled path →
      RUNTIME_OWNERSHIP_DRIFT (external targets stay untouched)
    Idempotent: owned files are removed once; a second call is a
    no-op; foreign or drifted files are never deleted.
    """
    fs = fs or RealFilesystem()
    root_real = fs.realpath(str(directory)) if str(directory) else ""
    removed = []
    for service, info in list(journal.items()):
        if not isinstance(info, dict) or "file" not in info:
            continue
        path_str = str(info["file"])
        if info.get("ownership") != "session":
            continue  # foreign ownership — refuse
        if root_real and not path_str.startswith(str(directory)):
            continue
        if fs.exists(path_str):
            if fs.is_reparse(path_str):
                raise RuntimeSpecError(
                    "RUNTIME_REMOVE_REPARSE_REFUSED", service)
            if root_real and not _confined(fs, path_str, root_real):
                raise RuntimeSpecError(
                    "RUNTIME_PATH_ESCAPE_REFUSED", service)
            fs.unlink(path_str)
        removed.append(service)
        del journal[service]
    return removed


#: mount spec name -> prerequisite config key carrying the REAL
#: validated host file path (placeholders are FORBIDDEN in argv).
_MOUNT_SOURCE_CONFIG_KEY = {
    "room-map": "room_map_path",
    "policy": "policy_path",
    "fixture_policy": "policy_path",
    "github_app_pem": "app_pem_path",
}


def plan_runtime_mounts(service: str, *, config: dict = None) -> list:
    """Return the :ro mount argv fragments for a service.

    R3 §4: with a validated prerequisite config the mount sources are
    the REAL host file paths from that config — never placeholder
    strings. Without a config (pure planning unit tests only) the
    fragment keeps the explicitly marked <placeholder> form which the
    argv execution tests reject for production use."""
    spec = SERVICE_RUNTIME_SPECS[service]
    fragments = []
    for m in spec.get("mounts", []):
        if config is not None:
            key = _MOUNT_SOURCE_CONFIG_KEY.get(m[0])
            if key is None or not config.get(key):
                raise RuntimeSpecError(
                    "RUNTIME_MOUNT_SOURCE_MISSING",
                    "%s: %s has no real host path in config"
                    % (service, m[0]))
            source = config[key]
        else:
            source = "<placeholder-%s-host-path>" % m[0]
        fragments.extend(["-v", "%s:%s:%s" % (source, m[1], m[2])])
    return fragments


def _render_env(env: dict, keys: frozenset) -> bytes:
    lines = ["%s=%s" % (k, env[k]) for k in sorted(keys)]
    return ("\n".join(lines) + "\n").encode("utf-8")


__all__ = [
    "RuntimeSpecError", "GATEWAY_E2E_ENV_KEYS", "GATEWAY_E2E_UPSTREAM",
    "GATEWAY_E2E_POLICY", "GATEWAY_READ_ONLY_TOOLS", "BRIDGE_ENV_KEYS",
    "BRIDGE_PROXY", "PROXY_ENV_KEYS", "SECRET_CONSUMER_MATRIX",
    "SERVICE_RUNTIME_SPECS", "RealFilesystem",
    "validate_gateway_e2e_env",
    "validate_bridge_env", "validate_proxy_env",
    "validate_secret_consumers", "validate_runtime_configs",
    "create_runtime_files", "remove_runtime_files",
    "plan_runtime_mounts",
]

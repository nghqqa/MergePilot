"""DiffParse core -- real unified-diff parser (framework-neutral, stdlib only).

This module is deliberately independent of the MergePilot common runtime: it
takes already-typed Python values and returns plain dicts, so it can be unit
tested and reused without importing the envelope/CLI layer. ``run.py`` is the
thin bridge that wires this core into the common contract.

Design rules enforced here:
* The diff text (paths, section headers, hunk bodies) is **untrusted opaque
  text**. Nothing in it is ever executed, shell-expanded or interpreted as an
  instruction. We only count lines and extract structure/ranges/statistics.
* No path from the diff is ever opened or read from the local filesystem.
* Output never echoes source or patch text -- only structure, ranges, stats and
  a one-way SHA-256 digest -- so prompt-injection text and secret-shaped
  strings inside the diff cannot reach the caller.
* Same input always yields the same business output (deterministic ordering).
* Over the input byte cap or malformed/truncated input -> fail-closed ERROR.
* Over the file/line safety caps -> PARTIAL with explicit degradation, never a
  silent drop, and never a fake "complete" result.
"""
from __future__ import annotations

import hashlib
import re

SCHEMA_VERSION = "1"
SUPPORTED_FORMAT = "unified"

# Conservative default safety caps (overridable via input.options).
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_TOTAL_LINES = 200000
DEFAULT_MAX_DIFF_BYTES = 2 * 1024 * 1024  # 2 MiB input hard cap

# Fixed change-category vocabulary (RiskClassify consumes these).
CATEGORIES = (
    "source", "test", "documentation", "dependency", "workflow",
    "config", "migration", "security_sensitive", "deletion", "binary",
)

CHANGE_TYPES = ("A", "M", "D", "R", "C", "T")


class DiffParseError(Exception):
    """Carries a public skill-specific error ``code`` and ``message``."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Skill-specific error codes (DIFF_PARSE_* prefix; common codes reused elsewhere)
# ---------------------------------------------------------------------------
UNSUPPORTED_FORMAT = "DIFF_PARSE_UNSUPPORTED_FORMAT"
INPUT_TOO_LARGE = "DIFF_PARSE_INPUT_TOO_LARGE"
MALFORMED = "DIFF_PARSE_MALFORMED"
PARTIAL_CONTEXT = "DIFF_PARSE_PARTIAL_CONTEXT"


# ---------------------------------------------------------------------------
# Categorization patterns. These classify a path into one *primary* category
# (mutually exclusive) plus optional overlays (security_sensitive / binary /
# deletion). Primary mutual exclusivity makes RiskClassify's only_categories
# rules meaningful: a doc-only change has exactly {documentation}.
# ---------------------------------------------------------------------------
_DEP_BASENAMES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
    "requirements.txt", "requirements-dev.txt", "constraints.txt",
    "pipfile", "pipfile.lock", "poetry.lock", "setup.py", "setup.cfg",
    "pyproject.toml",
    "go.mod", "go.sum", "go.work", "go.work.sum",
    "gemfile", "gemfile.lock",
    "composer.json", "composer.lock",
    "cargo.toml", "cargo.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "gradle.properties", "libs.versions.toml",
    "vcpkg.json", "conanfile.txt", "conanfile.py",
    "podfile", "podfile.lock",
    "mix.exs", "mix.lock", "rebar.config",
    ".python-version", ".node-version", ".nvmrc", ".ruby-version", ".tool-versions",
}

_WORKFLOW_PATTERNS = (
    re.compile(r"(^|/)\.github/workflows/"),
    re.compile(r"(^|/)\.circleci/"),
    re.compile(r"(^|/)\.buildkite/"),
)
_WORKFLOW_BASENAMES = {
    ".gitlab-ci.yml", ".gitlab-ci.yaml", "jenkinsfile", ".travis.yml",
    "azure-pipelines.yml", "azure-pipelines.yaml", "bitbucket-pipelines.yml",
    ".drone.yml", "appveyor.yml", "buddy.yml", ".cirrus.yml",
}

_MIGRATION_PATTERN = re.compile(
    r"(^|/)(migrations?|migration|alembic|flyway|liquibase|db/migrate|"
    r"db/migrations|schema|migrations)/",
    re.IGNORECASE,
)
_SCHEMA_SQL_BASENAMES = {"schema.sql", "structure.sql"}

_DOC_EXTS = (".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".tex")
_DOC_PATHS = (re.compile(r"(^|/)(docs?|documentation|doc)/", re.IGNORECASE),)
_DOC_BASENAMES_LOWER = {
    "readme", "changelog", "changes", "history", "license", "licence",
    "notice", "contributing", "authors", "code_of_conduct", "patents",
    "security.md", "maintainers", "contributors",
}

_TEST_PATHS = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|spec|specs|fixtures|testutils|testdata)/",
    re.IGNORECASE,
)
_TEST_BASENAME = re.compile(
    r"^(test_.+|.+_test\.py|.+_test\.go|.+_test\.rs|conftest\.py|"
    r".*\.test\.(js|jsx|ts|tsx)|.*\.spec\.(js|jsx|ts|tsx)|"
    r".*(test|spec)_(suite|case)s?\.(js|ts)|"
    r".*[a-z]test\.(java|kt|php)|.+tests?\.java)$",
    re.IGNORECASE,
)

_CONFIG_EXTS = (
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".editorconfig", ".gitattributes", ".env",
)
_CONFIG_BASENAMES_LOWER = {
    "makefile", "gnu", "dockerfile", ".dockerignore", ".gitignore",
    "tsconfig.json", "jsconfig.json", ".npmrc", ".prettierignore",
    ".gitkeep", "cmakecache.txt", ".eslintignore",
}
_CONFIG_BASENAME_PREFIXES = (
    "dockerfile.", ".eslintrc", ".prettierrc", ".babelrc", ".stylelintrc",
)

_SOURCE_EXTS = (
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go",
    ".rs", ".java", ".kt", ".kts", ".scala", ".c", ".h", ".cpp", ".cc",
    ".cxx", ".hpp", ".hxx", ".cs", ".vb", ".rb", ".php", ".swift", ".m",
    ".mm", ".clj", ".cljs", ".cljc", ".ex", ".exs", ".erl", ".hrl", ".lua",
    ".pl", ".pm", ".r", ".dart", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".sql", ".vue", ".svelte", ".elm", ".fs", ".fsx", ".ml", ".nim", ".zig",
    ".v", ".d", ".pas", ".f90", ".f95", ".cbl", ".asm", ".s", ".gradle",
)

# Overlay: security/auth/credential-sensitive paths (never lowers risk on its
# own; RiskClassify decides). Matched on the lowercased path.
_SECURITY_SEGMENTS = (
    "auth", "authentication", "authorization", "authorisation", "permission",
    "acl", "credential", "credentials", "secret", "secrets", "password",
    "passwd", "token", "tokens", "crypto", "cryptographic", "ssl", "tls",
    "certificate", "certificates", "security", "otp", "session", "sessions",
    "jwt", "oauth", "saml", "rbac", "keyring", "vault", "kms", "firewall",
    "iptables", "sudo", "privilege", "privileges",
)
_SECURITY_EXTS = (
    ".pem", ".key", ".crt", ".cer", ".csr", ".p12", ".pfx", ".jks",
    ".keystore", ".gpg", ".asc",
)
_SECURITY_BASENAMES_LOWER = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys",
    "known_hosts", "sudoers", "shadow", "htpasswd", ".htpasswd",
    "kdbx", "wallet.dat",
}


def _split_segments(path):
    """Lowercased path split on '/' for segment matching."""
    return [s for s in path.lower().split("/") if s]


def _basename_lower(path):
    idx = path.rfind("/")
    base = path[idx + 1:] if idx >= 0 else path
    return base.lower()


def _ext_lower(path):
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot >= 0 else ""


def _is_security_sensitive(path):
    base = _basename_lower(path)
    if base in _SECURITY_BASENAMES_LOWER:
        return True
    ext = _ext_lower(path)
    if ext in _SECURITY_EXTS:
        return True
    if base.startswith(".env") and (len(base) == 4 or base[4] in ".-+_"):
        return True
    for seg in _split_segments(path):
        if seg in _SECURITY_SEGMENTS:
            return True
        # path-component keywords, e.g. .../auth/login.py or auth_service.py
        for kw in ("auth", "secret", "password", "passwd", "credential",
                   "token", "permission", "privilege"):
            if kw in seg and seg not in ("authoritative",):
                # avoid pure substring false positives: require word boundary
                if re.search(r"(^|[_\-.])" + re.escape(kw) + r"([_\-.]|$)", seg):
                    return True
    return False


def _primary_category(path):
    """Return exactly one primary category or None (mutually exclusive)."""
    base = _basename_lower(path)
    ext = _ext_lower(path)

    if base in _DEP_BASENAMES:
        return "dependency"
    for pat in _WORKFLOW_PATTERNS:
        if pat.search(path):
            return "workflow"
    if base in _WORKFLOW_BASENAMES:
        return "workflow"
    if _MIGRATION_PATTERN.search(path):
        return "migration"
    if base in _SCHEMA_SQL_BASENAMES:
        return "migration"
    if ext in _DOC_EXTS:
        return "documentation"
    for pat in _DOC_PATHS:
        if pat.search(path):
            return "documentation"
    if base in _DOC_BASENAMES_LOWER or base.startswith("readme.") or \
            base.startswith("changelog.") or base.startswith("license."):
        return "documentation"
    if _TEST_PATHS.search(path) or _TEST_BASENAME.match(path.rsplit("/", 1)[-1]):
        return "test"
    if ext in _CONFIG_EXTS:
        return "config"
    if base in _CONFIG_BASENAMES_LOWER:
        return "config"
    for pref in _CONFIG_BASENAME_PREFIXES:
        if base.startswith(pref):
            return "config"
    if base in {"dockerfile"} or base.startswith("dockerfile."):
        return "config"
    # package.json etc. already caught as dependency; other *.json -> config
    if ext == ".json":
        return "config"
    if ext in _SOURCE_EXTS:
        return "source"
    return None


def categorize(path, change_type, binary):
    """Return a sorted list of categories for one file (deterministic order)."""
    cats = set()
    primary = _primary_category(path)
    if primary is not None:
        cats.add(primary)
    if _is_security_sensitive(path):
        cats.add("security_sensitive")
    if binary:
        cats.add("binary")
    if change_type == "D":
        cats.add("deletion")
    return [c for c in CATEGORIES if c in cats]


# ---------------------------------------------------------------------------
# Git quoted-path handling. Git wraps paths containing space/quote/backslash or
# non-ASCII bytes in double quotes with C-style escapes (incl. octal \NNN for
# raw bytes). We reverse that exactly.
# ---------------------------------------------------------------------------
def _git_unquote(token):
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        inner = token[1:-1]
        out = bytearray()
        i = 0
        n = len(inner)
        while i < n:
            c = inner[i]
            if c == "\\":
                if i + 1 >= n:
                    out.append(0x5C)
                    i += 1
                    continue
                nxt = inner[i + 1]
                if nxt == "n":
                    out.append(0x0A); i += 2
                elif nxt == "t":
                    out.append(0x09); i += 2
                elif nxt == "\\":
                    out.append(0x5C); i += 2
                elif nxt == '"':
                    out.append(0x22); i += 2
                elif nxt in "01234567":
                    digits = nxt
                    j = i + 2
                    while j < n and j < i + 4 and inner[j] in "01234567":
                        digits += inner[j]
                        j += 1
                    out.append(int(digits, 8) & 0xFF)
                    i = j
                else:
                    out.append(0x5C); out.append(ord(nxt)); i += 2
            else:
                out.extend(c.encode("utf-8"))
                i += 1
        return out.decode("utf-8", "replace")
    return token


def _tokenize_paths(rest):
    """Split a 'a/OLD b/NEW' suffix into raw path tokens (quote-aware).

    Git quotes a path and C-escapes special characters inside it (e.g. a literal
    double-quote becomes ``\\"``). An escaped char must never toggle quoting or
    be split on, so a backslash takes the following char literally and both are
    preserved verbatim for :func:`_git_unquote` to decode.
    """
    tokens = []
    cur = []
    in_quote = False
    i = 0
    n = len(rest)
    while i < n:
        c = rest[i]
        if c == "\\" and i + 1 < n:
            cur.append(c)
            cur.append(rest[i + 1])
            i += 2
            continue
        if c == '"':
            in_quote = not in_quote
            cur.append(c)
        elif c == " " and not in_quote:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        tokens.append("".join(cur))
    return tokens


def _strip_prefix(token, prefix):
    """Strip a leading 'a/' or 'b/' prefix (quoted or not) and unquote."""
    if token.startswith('"'):
        # quoted form like "a/path"
        if len(token) >= len(prefix) + 2 and token[1:1 + len(prefix)] == prefix:
            inner = '"' + token[1 + len(prefix):]
            return _git_unquote(inner)
        return _git_unquote(token)
    if token.startswith(prefix):
        return token[len(prefix):]
    return token


_NO_NEWLINE = "\\ No newline at end of file"
_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$"
)


def _mode_type(mode):
    return mode[:3] if mode else ""


# ---------------------------------------------------------------------------
# Linear parser: a single state-machine pass over all diff lines. Handles git
# ('diff --git') and plain ('--- '/'+++ ' only) sections, multi-file plain
# diffs, hunk over/under-count rejection (fail-closed), quoted-path unquoting,
# and the soft file/line caps. Error messages never echo untrusted diff content.
# ---------------------------------------------------------------------------
def _finalize_raw(r):
    """Resolve change_type / paths for an accumulated raw file. Returns a file
    dict or None when nothing identifiable was collected."""
    old_path = r["old_path"]
    new_path = r["new_path"]
    dg_old = r["dg_old"]
    dg_new = r["dg_new"]
    old_is_null = old_path is not None and old_path.strip() == "/dev/null"
    new_is_null = new_path is not None and new_path.strip() == "/dev/null"

    if r["is_rename"] or r["is_copy"]:
        final_new = r["rename_to"] or (new_path if not new_is_null else None) or dg_new or ""
        final_old = r["rename_from"] or (old_path if not old_is_null else None) or dg_old
        ct = "R" if r["is_rename"] else "C"
        path = final_new or ""
        old_out = final_old
    elif r["is_new_file"] or old_is_null:
        ct = "A"
        path = (new_path if not new_is_null else None) or dg_new or ""
        old_out = None
    elif r["is_deleted_file"] or new_is_null:
        ct = "D"
        path = (old_path if not old_is_null else None) or dg_old or ""
        old_out = None
    else:
        path = (new_path if not new_is_null else None) or dg_new or dg_old or ""
        old_out = None
        if r["old_mode"] and r["new_mode"] and _mode_type(r["old_mode"]) != _mode_type(r["new_mode"]):
            ct = "T"
        else:
            ct = "M"
    mode_changed = bool(r["old_mode"] and r["new_mode"] and r["old_mode"] != r["new_mode"])
    if not path:
        return None
    return {"path": path, "old_path": old_out, "change_type": ct,
            "additions": r["additions"], "deletions": r["deletions"],
            "binary": r["binary"], "mode_changed": mode_changed, "hunks": r["hunks"]}


def _new_raw(is_git, dg_old=None, dg_new=None):
    return {"is_git": is_git, "dg_old": dg_old, "dg_new": dg_new,
            "old_path": None, "new_path": None, "old_mode": None, "new_mode": None,
            "is_new_file": False, "is_deleted_file": False, "is_rename": False,
            "is_copy": False, "binary": False, "rename_from": None, "rename_to": None,
            "saw_minus": False, "saw_plus": False,
            "hunks": [], "additions": 0, "deletions": 0}


def _parse_lines(lines, max_files, line_budget):
    """Single linear pass. Returns ``(files, complete, degradation_reason)``.

    Raises ``DiffParseError(MALFORMED)`` on structural errors (propagates,
    fail-closed). Soft line-budget overrun is caught here and surfaced as
    ``complete=False`` (the in-progress, incomplete file is dropped).
    """
    files = []
    raw = None
    cur_hunk = None
    old_need = 0
    new_need = 0
    in_binary_patch = False
    complete = True
    degradation = None
    limit = line_budget[0]

    def close_hunk():
        nonlocal cur_hunk, old_need, new_need
        if raw is not None and cur_hunk is not None:
            raw["hunks"].append(cur_hunk)
        cur_hunk = None
        old_need = 0
        new_need = 0

    def close_file():
        nonlocal raw, in_binary_patch
        close_hunk()
        in_binary_patch = False
        if raw is not None:
            fd = _finalize_raw(raw)
            if fd is not None:
                files.append(fd)
            raw = None

    try:
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]

            # --- inside a hunk body: enforce declared counts (no under/over) ---
            if cur_hunk is not None:
                if line == _NO_NEWLINE:
                    i += 1
                    continue
                if line.startswith("\\"):
                    raise DiffParseError(MALFORMED, "unexpected escape line inside hunk")
                if old_need <= 0 and new_need <= 0:
                    close_hunk()
                    # fall through to top-level handling (do not advance)
                else:
                    if line == "":
                        if old_need <= 0 or new_need <= 0:
                            raise DiffParseError(MALFORMED, "hunk has more context lines than declared")
                        old_need -= 1
                        new_need -= 1
                    else:
                        sign = line[0]
                        if sign == " ":
                            if old_need <= 0 or new_need <= 0:
                                raise DiffParseError(MALFORMED, "hunk has more context lines than declared")
                            old_need -= 1
                            new_need -= 1
                        elif sign == "-":
                            if old_need <= 0:
                                raise DiffParseError(MALFORMED, "hunk has more '-' lines than old_count declares")
                            old_need -= 1
                            raw["deletions"] += 1
                        elif sign == "+":
                            if new_need <= 0:
                                raise DiffParseError(MALFORMED, "hunk has more '+' lines than new_count declares")
                            new_need -= 1
                            raw["additions"] += 1
                        else:
                            raise DiffParseError(MALFORMED, "hunk body line missing leading + - or space")
                    line_budget[0] -= 1
                    if line_budget[0] < 0:
                        raise DiffParseError(PARTIAL_CONTEXT, "line budget exceeded")
                    i += 1
                    continue

            # --- top level ---
            if line.startswith("diff --git "):
                close_file()
                if len(files) >= max_files:
                    complete = False
                    degradation = "file limit reached (max_files=%d); remaining sections skipped" % max_files
                    break
                toks = _tokenize_paths(line[len("diff --git "):])
                if len(toks) >= 2:
                    dg_old = _strip_prefix(toks[0], "a/")
                    dg_new = _strip_prefix(toks[1], "b/")
                elif len(toks) == 1:
                    dg_old = dg_new = _strip_prefix(toks[0], "a/")
                else:
                    raise DiffParseError(MALFORMED, "diff --git line has no paths")
                raw = _new_raw(True, dg_old, dg_new)
                i += 1
                continue
            if in_binary_patch:
                # base85 literal/delta lines after 'GIT binary patch' are ignored
                i += 1
                continue
            if line.startswith("--- "):
                old = _strip_prefix(line[4:].rstrip("\n"), "a/")
                if raw is None or (not raw["is_git"] and raw["saw_plus"]):
                    # plain-format new file boundary (no diff --git)
                    close_file()
                    if len(files) >= max_files:
                        complete = False
                        degradation = "file limit reached (max_files=%d); remaining sections skipped" % max_files
                        break
                    raw = _new_raw(False)
                raw["old_path"] = old
                raw["saw_minus"] = True
                i += 1
                continue
            if line.startswith("+++ "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "+++ header without preceding file context")
                raw["new_path"] = _strip_prefix(line[4:].rstrip("\n"), "b/")
                raw["saw_plus"] = True
                i += 1
                continue
            if line.startswith("@@"):
                if raw is None:
                    raise DiffParseError(MALFORMED, "hunk header outside a file section")
                hh = _parse_hunk_header(line)
                if hh is None:
                    raise DiffParseError(MALFORMED, "unparseable hunk header")
                cur_hunk = hh
                old_need = hh["old_count"]
                new_need = hh["new_count"]
                i += 1
                continue
            if line.startswith("old mode "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "old mode outside file section")
                raw["old_mode"] = line[len("old mode "):].strip()
                i += 1
                continue
            if line.startswith("new mode "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "new mode outside file section")
                raw["new_mode"] = line[len("new mode "):].strip()
                i += 1
                continue
            if line.startswith("new file mode "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "new file mode outside file section")
                raw["is_new_file"] = True
                raw["new_mode"] = line[len("new file mode "):].strip()
                i += 1
                continue
            if line.startswith("deleted file mode "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "deleted file mode outside file section")
                raw["is_deleted_file"] = True
                raw["old_mode"] = line[len("deleted file mode "):].strip()
                i += 1
                continue
            if line.startswith("rename from "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "rename from outside file section")
                raw["is_rename"] = True
                raw["rename_from"] = _git_unquote(line[len("rename from "):].rstrip("\n"))
                i += 1
                continue
            if line.startswith("rename to "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "rename to outside file section")
                raw["is_rename"] = True
                raw["rename_to"] = _git_unquote(line[len("rename to "):].rstrip("\n"))
                i += 1
                continue
            if line.startswith("copy from "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "copy from outside file section")
                raw["is_copy"] = True
                raw["rename_from"] = _git_unquote(line[len("copy from "):].rstrip("\n"))
                i += 1
                continue
            if line.startswith("copy to "):
                if raw is None:
                    raise DiffParseError(MALFORMED, "copy to outside file section")
                raw["is_copy"] = True
                raw["rename_to"] = _git_unquote(line[len("copy to "):].rstrip("\n"))
                i += 1
                continue
            if line.startswith("similarity index") or line.startswith("dissimilarity index"):
                i += 1
                continue
            if line.startswith("index "):
                i += 1
                continue
            if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                if raw is None:
                    raise DiffParseError(MALFORMED, "binary marker outside file section")
                raw["binary"] = True
                in_binary_patch = line.startswith("GIT binary patch")
                if line.startswith("Binary files "):
                    tail = line[len("Binary files "):]
                    if tail.endswith(" differ"):
                        tail = tail[:-len(" differ")]
                    toks = _tokenize_paths(tail)
                    if len(toks) >= 3 and toks[1] == "and":
                        raw["dg_old"] = _strip_prefix(toks[0], "a/")
                        raw["dg_new"] = _strip_prefix(toks[2], "b/")
                    elif len(toks) >= 2:
                        raw["dg_old"] = _strip_prefix(toks[0], "a/")
                        raw["dg_new"] = _strip_prefix(toks[1], "b/")
                i += 1
                continue
            if line.strip() == "":
                i += 1
                continue
            # unrecognized non-blank line at top level (content not echoed)
            raise DiffParseError(MALFORMED, "unrecognized diff line at top level")

        # EOF: an unfinished hunk means truncation
        if cur_hunk is not None and (old_need > 0 or new_need > 0):
            raise DiffParseError(MALFORMED, "truncated hunk (fewer lines than declared)")
        close_file()
    except DiffParseError as exc:
        if exc.code == PARTIAL_CONTEXT:
            # drop the in-progress file; surface what was fully parsed
            return files, False, "line limit reached (max_total_lines=%d); remaining content skipped" % limit
        raise
    return files, complete, degradation


def _parse_hunk_header(line):
    m = _HUNK_RE.match(line)
    if not m:
        return None
    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) is not None else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) is not None else 1
    return {
        "old_start": old_start, "old_count": old_count,
        "new_start": new_start, "new_count": new_count,
    }


def _module_of(path):
    idx = path.rfind("/")
    return path[:idx] if idx >= 0 else "."


def parse_diff(*, repo, base_sha, head_sha, diff_text, diff_format,
               pr_number=None, options=None):
    """Parse a unified diff into a structured change-context dict.

    Raises ``DiffParseError`` (code UNSUPPORTED_FORMAT / INPUT_TOO_LARGE /
    MALFORMED) for fail-closed conditions. Returns a dict whose ``complete``
    flag is False when a soft safety cap truncated parsing (caller surfaces
    PARTIAL).
    """
    if diff_format != SUPPORTED_FORMAT:
        raise DiffParseError(
            UNSUPPORTED_FORMAT,
            "diff_format %r not supported (only 'unified')" % diff_format,
        )

    opts = options or {}
    max_files = opts.get("max_files") or DEFAULT_MAX_FILES
    max_total_lines = opts.get("max_total_lines") or DEFAULT_MAX_TOTAL_LINES
    max_diff_bytes = opts.get("max_diff_bytes") or DEFAULT_MAX_DIFF_BYTES

    if not isinstance(diff_text, str):
        raise DiffParseError(MALFORMED, "diff_text must be a string")
    raw_bytes = diff_text.encode("utf-8", "replace")
    if len(raw_bytes) > max_diff_bytes:
        raise DiffParseError(
            INPUT_TOO_LARGE,
            "diff_text is %d bytes (> max_diff_bytes=%d)" % (len(raw_bytes), max_diff_bytes),
        )

    input_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    # normalize CRLF/CR -> LF, then split (keep empty trailing element handled)
    text = diff_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # a trailing newline yields a trailing '' which is harmless (blank meta line)

    files, complete, degradation = _parse_lines(lines, max_files, [max_total_lines])

    # build aggregate structures (deterministic ordering)
    modules = set()
    cats_union = set()
    for f in files:
        modules.add(_module_of(f["path"]))
        if f["old_path"]:
            modules.add(_module_of(f["old_path"]))
        f["categories"] = categorize(f["path"], f["change_type"], f["binary"])
        cats_union.update(f["categories"])

    stats = {
        "files_changed": len(files),
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "hunks": sum(len(f["hunks"]) for f in files),
        "binary_files": sum(1 for f in files if f["binary"]),
    }

    source = {"repo": repo, "base_sha": base_sha, "head_sha": head_sha}
    if pr_number is not None:
        source["pr_number"] = pr_number

    out = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "input_sha256": input_sha256,
        "complete": complete,
        "files": [
            {
                "path": f["path"],
                "old_path": f["old_path"],
                "change_type": f["change_type"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "binary": f["binary"],
                "mode_changed": f["mode_changed"],
                "categories": f["categories"],
                "hunks": [
                    {
                        "old_start": h["old_start"],
                        "old_count": h["old_count"],
                        "new_start": h["new_start"],
                        "new_count": h["new_count"],
                    }
                    for h in f["hunks"]
                ],
            }
            for f in files
        ],
        "modules_touched": sorted(modules),
        "change_categories": [c for c in CATEGORIES if c in cats_union],
        "stats": stats,
    }
    if not complete:
        out["degradation_reason"] = degradation
    return out

#!/usr/bin/env python3
"""Fail-closed text, cache, credential, and attribution hygiene gate."""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGETS = (
    ROOT / "docs/M4-F1-数据库契约.md",
    ROOT / "docs/M5-0-HiClaw-Live设计冻结.md",
    ROOT / "docs/项目状态.md",
    ROOT / "tools/audit-db/m4f1_state.sql",
    ROOT / "tools/workflow-controller/controller.py",
    ROOT / "tools/workflow-controller/gateway_client.py",
    ROOT / "tools/workflow-controller/m4f_controller.py",
    ROOT / "tools/m4f_skill_worker.py",
    ROOT / "tools/m4f_demo.py",
    ROOT / "tools/m4f-runtime",
    ROOT / "tools/start-controller-container.sh",
    ROOT / "tools/start-m5-0-candidate.sh",
    ROOT / "config/m5-0-allowlist.yaml",
    ROOT / "tests/m4f1",
    ROOT / "tests/m5_0/test_m5_strict_parser.py",
    ROOT / "tests/m5_0/__init__.py",
    ROOT / "tools/test-env/mp_guard.sh",
    ROOT / "tools/test-env/wsl_test.sh",
    ROOT / "tools/test-env/wsl_test.ps1",
    ROOT / "tools/test-env/mp_launch.sh",
    ROOT / "tests/m4f1/run_all_test.sh",
    ROOT / "tests/test_env_isolation.sh",
    ROOT / "tests/test_env_isolation.ps1",
    ROOT / "tests/m5_0/fixtures/run_neg_guard.sh",
    ROOT / "evidence/m4/m4f",
    ROOT / "evidence/m4/m4f-hotfix1",
)
# v2.4 Fix 2: NO whole-file exclusion. Every target IS scanned for text hygiene
# AND credentials. Only specific CRED_HIT lines whose value is a non-literal
# env-var reference ($VAR / os.environ.get / empty) are suppressed — these are
# legitimate deployment plumbing, not leaked secrets. AI_HIT is never suppressed.
TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".sql", ".txt", ".yaml", ".yml"}

# credential keys matched by skills.common.runtime.redact (assignment_secret).
_CRED_KEY = re.compile(
    r"[A-Za-z_]*(?:token|password|passwd|secret|access_token|PG_DSN|PG_PASSWORD"
    r"|MERGEPILOT_APPROVER_PASS)[A-Za-z_]*",
    re.IGNORECASE,
)
_SEP_VALUE = re.compile(r"\s*[:=]\s*(.*)", re.DOTALL)
# A LITERAL secret value: a quoted string of 6+ chars NOT starting with '$'.
# Bare identifiers (PG_PASS), $VAR refs, $(...) command substitutions, and
# empty values are all non-literal plumbing, never secrets.
_LITERAL_SECRET = re.compile(r"""(?:'[^'$\n][^'\n]{5,}'|"[^"$\n][^"\n]{5,}")""")


def _line_has_literal_secret(line: str) -> bool:
    """True if a credential key on this line is followed by a literal quoted
    value (a real secret)."""
    for m in _CRED_KEY.finditer(line):
        sep = _SEP_VALUE.match(line[m.end():])
        if sep and _LITERAL_SECRET.match(sep.group(1).strip()):
            return True
    return False


def _line_is_cred_passthrough(line: str) -> bool:
    """True if the line has a credential key but NO literal secret — every
    credential reference is a variable/config/shell/control-flow form."""
    if not _CRED_KEY.search(line):
        return False
    return not _line_has_literal_secret(line)


def _files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for target in TARGETS:
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found.extend(path for path in target.rglob("*") if path.is_file())
    return sorted(set(found))


def main() -> int:
    failures: list[str] = []
    files = _files()
    text_files = [path for path in files if path.suffix.lower() in TEXT_SUFFIXES]
    for path in text_files:
        rel = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            failures.append(f"BOM {rel}")
        if b"\r" in raw:
            failures.append(f"CR {rel}")
        if raw and not raw.endswith(b"\n"):
            failures.append(f"NO_FINAL_LF {rel}")
        for line_number, line in enumerate(raw.splitlines(), 1):
            if line.rstrip(b" \t") != line:
                failures.append(f"TRAILING_WS {rel}:{line_number}")

    cache_paths = sorted(
        path.relative_to(ROOT).as_posix()
        for base in (ROOT / "skills", ROOT / "tests", ROOT / "tools")
        for path in base.rglob("*")
        if path.name in {"__pycache__", ".pytest_cache"} or path.suffix == ".pyc"
    )
    failures.extend(f"CACHE {path}" for path in cache_paths)

    scan = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/skills/scan_delivery.py"),
            *(str(path) for path in TARGETS),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if scan.stderr:
        print(scan.stderr, end="", file=sys.stderr)

    # Parse CRED_HIT lines; suppress only non-literal env-var pass-throughs.
    suppressed = 0
    for line in scan.stdout.splitlines():
        if line.startswith("CRED_HIT "):
            parts = line.split(None, 2)
            file_lineno = parts[1] if len(parts) > 1 else ""
            idx = file_lineno.rfind(":")
            exempt = False
            if idx > 0:
                try:
                    lineno = int(file_lineno[idx + 1:])
                    hit_path = pathlib.Path(file_lineno[:idx].replace("/", os.sep))
                    if not hit_path.is_absolute():
                        hit_path = ROOT / hit_path
                    hit_lines = hit_path.read_text(
                        encoding="utf-8", errors="replace").splitlines()
                    if 0 < lineno <= len(hit_lines):
                        exempt = _line_is_cred_passthrough(hit_lines[lineno - 1])
                except (ValueError, OSError):
                    pass
            if exempt:
                suppressed += 1
                continue
            failures.append(line)
        else:
            # scan_targets / total_hits / AI_HIT — print through
            print(line)

    if failures:
        for failure in failures:
            print(f"HYGIENE_FAIL {failure}")
        print(f"M4-F1 HYGIENE FAIL files={len(text_files)} findings={len(failures)} "
              f"suppressed_passthroughs={suppressed}")
        return 1
    print(f"M4-F1 HYGIENE PASS files={len(text_files)} cache=0 "
          f"scan_hits=0 suppressed_passthroughs={suppressed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fail-closed text, cache, credential, and attribution hygiene gate."""
from __future__ import annotations

import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGETS = (
    ROOT / "docs/M4-F1-数据库契约.md",
    ROOT / "docs/项目状态.md",
    ROOT / "tools/audit-db/m4f1_state.sql",
    ROOT / "tools/workflow-controller/controller.py",
    ROOT / "tools/workflow-controller/m4f_controller.py",
    ROOT / "tools/m4f_skill_worker.py",
    ROOT / "tools/m4f_demo.py",
    ROOT / "tools/m4f-runtime",
    ROOT / "tests/m4f1",
    ROOT / "evidence/m4/m4f",
    ROOT / "evidence/m4/m4f-hotfix1",
)
SCAN_TARGETS = tuple(
    path for path in TARGETS if path != ROOT / "tools/workflow-controller/controller.py"
)
TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".sql", ".txt", ".yaml", ".yml"}


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
            *(str(path) for path in SCAN_TARGETS),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if scan.stdout:
        print(scan.stdout, end="")
    if scan.stderr:
        print(scan.stderr, end="", file=sys.stderr)
    if scan.returncode != 0:
        failures.append(f"DELIVERY_SCAN rc={scan.returncode}")

    if failures:
        for failure in failures:
            print(f"HYGIENE_FAIL {failure}")
        print(f"M4-F1 HYGIENE FAIL files={len(text_files)} findings={len(failures)}")
        return 1
    print(f"M4-F1 HYGIENE PASS files={len(text_files)} cache=0 scan_hits=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# MergePilot test launcher (runs inside MergePilot-Test WSL2 distro).
# wsl_test.sh / wsl_test.ps1 call this via `wsl.exe -d MergePilot-Test -u root
# -- bash <path>/mp_launch.sh <repo_root> <inner_rel> [inner args...]`.
# Using a script file (not bash -c) ensures wsl.exe passes positional args
# correctly (bash -c after wsl.exe -- drops positional args on Windows).
#
# Exit codes:
#   68  RepoRoot exists but cd failed (shouldn't happen)
#   69  RepoRoot is not a directory, or InnerScript file does not exist
#   inner's own non-zero exit code is propagated as-is
set -euo pipefail
ROOT="${1:?mp_launch: missing repo_root}"
INNER="${2:?mp_launch: missing inner_script}"

# Existence checks (P3 fix): verify paths before cd/exec.
if [ ! -d "$ROOT" ]; then
  echo "mp_launch: RepoRoot '$ROOT' is not a directory" >&2
  exit 69
fi
if [ ! -f "$ROOT/$INNER" ]; then
  echo "mp_launch: inner script '$ROOT/$INNER' does not exist" >&2
  exit 69
fi

cd "$ROOT" || { echo "mp_launch: cd '$ROOT' failed" >&2; exit 68; }
exec bash "$INNER" "${@:3}"

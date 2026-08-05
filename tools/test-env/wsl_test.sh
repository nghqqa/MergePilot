#!/usr/bin/env bash
# MergePilot unified safe entry — the ONLY sanctioned way to run a MergePilot
# test script that needs Docker. Forces wsl.exe -d MergePilot-Test -u root and
# invokes the test-daemon guard. Windows/Git Bash top-level wrappers MUST call
# this instead of hand-rolling their own wsl.exe -d MergePilot-Test line.
#
# Usage: wsl_test.sh <repo_root_wsl> <inner_script_relpath> [inner args...]
#   repo_root_wsl        absolute WSL path of the repo (e.g. /mnt/d/goai/mergepilot-os)
#   inner_script_relpath path RELATIVE to repo_root (e.g. tests/m5_0/_inner.sh)
#
# Safety:
#   - argument-array passing (no bash -lc string concatenation, no eval)
#   - rejects repo roots / inner paths that are absolute, contain '..', or do
#     not exist (existence verified by mp_launch.sh: rc=69 if missing)
#   - forces -d MergePilot-Test -u root (never the default Ubuntu-22.04 distro)
#   - the inner script MUST source tools/test-env/mp_guard.sh at its top; this
#     wrapper itself never invokes docker
#   - propagates the inner script's exact exit code
set -euo pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

if [ "$#" -lt 2 ]; then
  echo "usage: wsl_test.sh <repo_root_wsl> <inner_script_relpath> [inner args...]" >&2
  exit 64
fi

ROOT="${1:?missing repo_root_wsl}"
INNER_REL="${2:?missing inner_script_relpath}"
shift 2

# ── path validation (reject escape / traversal) ──
case "$ROOT" in
  /*) : ;;                                  # must be absolute WSL path
  *) echo "wsl_test: repo_root must be an absolute WSL path (got '$ROOT')" >&2; exit 65 ;;
esac
case "$INNER_REL" in
  /*) echo "wsl_test: inner_script must be relative (got '$INNER_REL')" >&2; exit 66 ;;
  *) : ;;
esac
if [[ "$INNER_REL" == *'..'* || "$ROOT" == *'..'* ]]; then
  echo "wsl_test: '..' is forbidden in repo_root or inner_script" >&2; exit 67
fi

# Existence check on the Git Bash side (before calling wsl.exe) so a fake
# RepoRoot or missing inner script returns rc=69, not a wsl.exe internal error.
# Convert /mnt/d/X → /d/X for Git Bash filesystem access.
_gb_root="${ROOT/\/mnt\//\/}"
if [ ! -d "$_gb_root" ]; then
  echo "wsl_test: RepoRoot '$ROOT' is not a directory" >&2; exit 69
fi
if [ ! -f "$_gb_root/$INNER_REL" ]; then
  echo "wsl_test: inner script '$ROOT/$INNER_REL' does not exist" >&2; exit 69
fi

# Launch via a script FILE (not bash -c) because wsl.exe drops positional args
# after `bash -c '...'` on Windows. mp_launch.sh cd's into the repo + execs the
# inner script with remaining args. No eval, no string concatenation.
exec wsl.exe -d MergePilot-Test -u root \
  -- bash "$ROOT/tools/test-env/mp_launch.sh" "$ROOT" "$INNER_REL" "$@"

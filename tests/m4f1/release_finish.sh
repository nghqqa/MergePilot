#!/usr/bin/env bash
# release_finish.sh — shared, sourceable fail-closed release verification.
#
# run_all.sh sources this for its EXIT trap; the negative counterexample gate
# (run_release_evidence_negatives.sh) sources the SAME function so the
# fail-closed wiring is exercised without running the full disposable Docker
# gate suite.
#
#   release_finish <business_rc> <gate_log> <evidence> <verification> <repo_root>
#
# Returns (via $?) final_rc = business_rc != 0 ? business_rc : writer_rc.
# A verification-writer / digest / JSON failure is therefore fail-closed even
# when every business gate passed (closes the fail-open gap). The caller is
# expected to have cleared <verification> at run start so a writer crash cannot
# leave a stale green record from a previous run.

release_finish() {
  local business_rc="$1"
  local gate_log="$2"
  local evid="$3"
  local verification="$4"
  local root="$5"
  local vfy_log
  vfy_log="$(mktemp /tmp/m4f-vfy.XXXXXX)"
  local vrc=0
  mkdir -p "$(dirname "$verification")"
  python3 "$root/tests/m4f1/write_verification.py" \
    "$gate_log" "$evid" "$verification" "$root" >"$vfy_log" 2>&1 || vrc=$?
  if [ "$vrc" -ne 0 ]; then
    echo "[m4f1] verification writer rc=$vrc:" >&2
    cat "$vfy_log" >&2 || true
  fi
  rm -f "$vfy_log"
  if [ -f "$verification" ]; then
    cat "$verification"
  fi
  local final_rc="$business_rc"
  if [ "$final_rc" -eq 0 ]; then
    final_rc="$vrc"
  fi
  if [ "$final_rc" -ne 0 ]; then
    echo "M4-F1 GATES FAILED rc=$final_rc" >&2
  fi
  return "$final_rc"
}

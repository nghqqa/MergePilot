#!/usr/bin/env bash
# install_guarded_startup.sh -- thin wrapper around install_guarded.py.
#
# DEFAULT IS DRY-RUN. Pass --apply to execute the transactional install.
# All transactional logic (verify, save policies, set restart=no, atomic
# unit install, daemon-reload/enable/is-enabled, rollback) lives in the
# testable Python core.
#
# Environment:
#   MP_SYSTEMD_UNIT_DIR  (default /etc/systemd/system)
#   MP_MANAGED_FILE      (default /etc/hiclab/managed-containers)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/install_guarded.py" "$@"

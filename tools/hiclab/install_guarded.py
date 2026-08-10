#!/usr/bin/env python3
"""Transactional guarded-startup installer (testable core).

Every step is all-or-nothing. A failure at ANY step rolls back ALL prior
changes to their EXACT prior state:
  * every container's original RestartPolicy
  * managed-containers file: original bytes + mode (or removed if absent)
  * systemd unit file: original bytes + mode (or removed if absent)
  * unit enabled/disabled/not-found state
  * daemon-reload executed
  * post-rollback verification (digest + policy + enabled state)

Rollback's OWN failure returns an independent non-zero status (2) and a
visible failure list -- it is never silent.

All Docker, filesystem, and systemctl operations go through injectable
classes so the full transaction is unit-testable on the host.

Status codes (main): 0 = applied; 1 = apply failed, rolled back cleanly;
2 = apply failed AND rollback also failed.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import managed_containers as mc  # noqa: E402

UNIT_NAME = "hiclab-guarded-start.service"


def _unit_content(supervisor_path):
    return (
        "[Unit]\n"
        "Description=HiClaw guarded startup (disk-guard gated)\n"
        "After=docker.service\n"
        "Requires=docker.service\n"
        "ConditionPathExists=%s\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=%s\n"
        "RemainAfterExit=yes\n"
        "Restart=no\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ) % (supervisor_path, supervisor_path)


def _sha(data):
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


class FsOps:
    """Filesystem operations (real impl; injectable in tests)."""

    def read_with_mode(self, path):
        """Return (existed: bool, content_bytes, mode_int)."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return (False, None, None)
        with open(path, "rb") as fh:
            content = fh.read()
        return (True, content, st.st_mode & 0o777)

    def atomic_write(self, path, content_bytes, mode=None):
        tmp = path + ".tmp.%d" % os.getpid()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, content_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True

    def remove(self, path):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True


class DockerOps:
    def __init__(self):
        import subprocess
        self._subprocess = subprocess

    def exists(self, name):
        r = self._subprocess.run(["docker", "inspect", name],
                                 capture_output=True)
        return r.returncode == 0

    def get_restart_policy(self, name):
        r = self._subprocess.run(
            ["docker", "inspect", "-f",
             "{{.HostConfig.RestartPolicy.Name}}", name],
            capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None

    def set_restart(self, name, policy):
        r = self._subprocess.run(
            ["docker", "update", "--restart=%s" % policy, name],
            capture_output=True)
        return r.returncode == 0


class SystemdOps:
    def __init__(self):
        import subprocess
        self._subprocess = subprocess

    def get_enabled_state(self, unit):
        """Return the raw is-enabled state string: 'enabled', 'disabled',
        'masked', 'static', 'indirect', or 'not-found'. static/indirect/
        masked are NOT collapsed to 'enabled' -- callers decide what to
        support."""
        r = self._subprocess.run(["systemctl", "is-enabled", unit],
                                 capture_output=True, text=True)
        out = (r.stdout or "").strip().lower()
        if out in ("enabled", "disabled", "masked", "static", "indirect"):
            return out
        if r.returncode == 0 and out:
            return out
        return "not-found"

    def daemon_reload(self):
        return self._subprocess.run(["systemctl", "daemon-reload"],
                                    capture_output=True).returncode == 0

    def enable(self, unit):
        return self._subprocess.run(["systemctl", "enable", unit],
                                    capture_output=True).returncode == 0

    def disable(self, unit):
        r = self._subprocess.run(["systemctl", "disable", unit],
                                 capture_output=True)
        return r.returncode == 0

    def is_enabled(self, unit):
        return self._subprocess.run(["systemctl", "is-enabled", unit],
                                    capture_output=True).returncode == 0


def take_snapshot(managed, docker, fs, systemd, managed_file, unit_path):
    """Capture the full pre-apply state for exact rollback."""
    snap = {"policies": {}, "managed_file": {}, "unit_file": {},
            "unit_enabled": "not-found"}
    for n in managed:
        pol = docker.get_restart_policy(n)
        snap["policies"][n] = pol if pol is not None else "no"
    ex, content, mode = fs.read_with_mode(managed_file)
    snap["managed_file"] = {"existed": ex, "bytes": content, "mode": mode,
                            "digest": _sha(content)}
    ex, content, mode = fs.read_with_mode(unit_path)
    snap["unit_file"] = {"existed": ex, "bytes": content, "mode": mode,
                         "digest": _sha(content)}
    snap["unit_enabled"] = systemd.get_enabled_state(unit_path.rsplit("/", 1)[-1]
                                                     if "/" in unit_path
                                                     else UNIT_NAME)
    return snap


SUPPORTED_ENABLED_STATES = ("enabled", "disabled", "not-found")


def _verify_restored(snap, docker, fs, systemd, managed_file, unit_path,
                     unit_name):
    """Return list of mismatch strings (empty = fully verified).

    Compares: every restart policy, managed+unit file bytes digest AND mode,
    and the exact unit enabled state.
    """
    failures = []
    for n, pol in snap["policies"].items():
        try:
            actual = docker.get_restart_policy(n)
        except Exception as exc:
            failures.append("policy %s: get raised %s" % (n, exc))
            continue
        if (actual or "no") != pol:
            failures.append("policy %s: %s != %s" % (n, actual, pol))
    mf = snap["managed_file"]
    try:
        ex, content, mode = fs.read_with_mode(managed_file)
    except Exception as exc:
        failures.append("managed file read raised: %s" % exc)
        ex, content, mode = False, None, None
    if mf["existed"]:
        if not ex or _sha(content) != mf["digest"]:
            failures.append("managed file digest mismatch")
        if ex and mode != mf["mode"]:
            failures.append("managed file mode %o != %o" % (mode, mf["mode"]))
    else:
        if ex:
            failures.append("managed file should be absent")
    uf = snap["unit_file"]
    try:
        ex, content, mode = fs.read_with_mode(unit_path)
    except Exception as exc:
        failures.append("unit file read raised: %s" % exc)
        ex, content, mode = False, None, None
    if uf["existed"]:
        if not ex or _sha(content) != uf["digest"]:
            failures.append("unit file digest mismatch")
        if ex and mode != uf["mode"]:
            failures.append("unit file mode %o != %o" % (mode, uf["mode"]))
    else:
        if ex:
            failures.append("unit file should be absent")
    try:
        actual_en = systemd.get_enabled_state(unit_name)
    except Exception as exc:
        failures.append("enabled state read raised: %s" % exc)
        actual_en = "<error>"
    if actual_en != snap["unit_enabled"]:
        failures.append("enabled state: %s != %s"
                        % (actual_en, snap["unit_enabled"]))
    return failures


def rollback(snap, docker, fs, systemd, managed_file, unit_path, unit_name):
    """Full restore to snapshot. Returns (ok: bool, failures: list).

    Every step is wrapped so exceptions are recorded as failures (never
    propagated). Restores policies, managed+unit file bytes+mode, unit
    enabled state; runs daemon-reload; verifies the result.
    """
    failures = []

    def _record(label, fn, *a, **kw):
        try:
            if not fn(*a, **kw):
                failures.append(label)
        except Exception as exc:
            failures.append("%s raised: %s" % (label, exc))

    for n, pol in snap["policies"].items():
        _record("restore policy %s" % n, docker.set_restart, n, pol)
    mf = snap["managed_file"]
    if mf["existed"]:
        _record("restore managed file", fs.atomic_write, managed_file,
                mf["bytes"], mode=mf["mode"])
    else:
        _record("remove managed file", fs.remove, managed_file)
    uf = snap["unit_file"]
    if uf["existed"]:
        _record("restore unit file", fs.atomic_write, unit_path, uf["bytes"],
                mode=uf["mode"])
    else:
        _record("remove unit file", fs.remove, unit_path)
    _record("daemon-reload during rollback", systemd.daemon_reload)
    orig_enabled = snap["unit_enabled"]
    if orig_enabled == "enabled":
        _record("re-enable unit", systemd.enable, unit_name)
    elif orig_enabled in ("disabled", "not-found"):
        _record("disable unit", systemd.disable, unit_name)
    try:
        failures.extend(_verify_restored(snap, docker, fs, systemd,
                                         managed_file, unit_path, unit_name))
    except Exception as exc:
        failures.append("verify raised: %s" % exc)
    return (len(failures) == 0, failures)


def dry_run(docker_ops=None, fs_ops=None, unit_path=None, managed_file=None,
            supervisor_path=None):
    docker_ops = docker_ops or DockerOps()
    lines = ["=== install_guarded DRY-RUN (no changes) ==="]
    lines.append("managed containers (order): %s" % ", ".join(mc.names()))
    lines.append("excluded (do NOT exist): %s" % ", ".join(mc.EXCLUDED))
    missing = [n for n in mc.names() if not docker_ops.exists(n)]
    if missing:
        lines.append("MISSING (apply would FAIL): %s" % ", ".join(missing))
    else:
        lines.append("all managed containers present")
    for n in mc.names():
        rp = docker_ops.get_restart_policy(n)
        lines.append("  %s: current restart=%s -> no" % (n, rp))
    lines.append("unit target: %s" % (unit_path or "(default)"))
    lines.append("managed file: %s" % (managed_file or "(default)"))
    lines.append("(re-run with --apply during a maintenance window)")
    return "\n".join(lines)


def apply(docker_ops, fs_ops, systemd_ops, unit_path, managed_file,
          supervisor_path):
    """Run the full transaction.

    Returns (status, detail, rollback_failures) where:
      status 0 = applied
      status 1 = apply failed, rolled back cleanly
      status 2 = apply failed AND rollback also failed
    """
    try:
        mc.check_unique()
        mc.validate_no_excluded()
    except ValueError as exc:
        return (1, "manifest invalid: %s" % exc, [])

    managed = mc.names()
    missing = [n for n in managed if not docker_ops.exists(n)]
    if missing:
        return (1, "missing containers (nothing changed): %s"
                % ", ".join(missing), [])

    snap = take_snapshot(managed, docker_ops, fs_ops, systemd_ops,
                         managed_file, unit_path)

    # Refuse unsupported unit enabled states (masked/static/indirect) BEFORE
    # making any change. take_snapshot is read-only, so nothing to roll back.
    if snap["unit_enabled"] not in SUPPORTED_ENABLED_STATES:
        return (1, "unsupported unit enabled state %r (masked/static/indirect "
                    "not handled); apply refused, nothing changed"
                % snap["unit_enabled"], [])

    def fail_and_rollback(reason):
        try:
            rb_ok, rb_failures = rollback(snap, docker_ops, fs_ops,
                                          systemd_ops, managed_file,
                                          unit_path, UNIT_NAME)
        except Exception as exc:
            return (2, "%s; ROLLBACK RAISED: %s" % (reason, exc), [str(exc)])
        if rb_ok:
            return (1, "%s; rolled back cleanly (state verified)" % reason, [])
        return (2, "%s; ROLLBACK ALSO FAILED: %s"
                % (reason, "; ".join(rb_failures)), rb_failures)

    try:
        for n in managed:
            if not docker_ops.set_restart(n, "no"):
                return fail_and_rollback(
                    "docker update --restart=no failed: %s" % n)
        managed_content = ("\n".join(managed) + "\n").encode("utf-8")
        if not fs_ops.atomic_write(managed_file, managed_content):
            return fail_and_rollback("managed file write failed")
        unit_bytes = _unit_content(supervisor_path).encode("utf-8")
        if not fs_ops.atomic_write(unit_path, unit_bytes):
            return fail_and_rollback("unit file write failed")
        if not systemd_ops.daemon_reload():
            return fail_and_rollback("systemctl daemon-reload failed")
        if not systemd_ops.enable(UNIT_NAME):
            return fail_and_rollback("systemctl enable failed")
        if not systemd_ops.is_enabled(UNIT_NAME):
            return fail_and_rollback("systemctl is-enabled verify failed")
        bad = [n for n in managed
               if docker_ops.get_restart_policy(n) != "no"]
        if bad:
            return fail_and_rollback(
                "post-install audit: restart!=no for %s" % ", ".join(bad))
    except Exception as exc:
        return fail_and_rollback("exception during apply: %s" % exc)

    return (0, "installed: %d containers restart=no, unit enabled" % len(managed),
            [])


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    do_apply = "--apply" in argv
    unit_dir = os.environ.get("MP_SYSTEMD_UNIT_DIR", "/etc/systemd/system")
    unit_path = os.path.join(unit_dir, UNIT_NAME)
    managed_file = os.environ.get(
        "MP_MANAGED_FILE", "/etc/hiclab/managed-containers")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    supervisor_path = os.path.join(script_dir, "hiclab_supervisor.sh")

    if not do_apply:
        sys.stdout.write(
            dry_run(unit_path=unit_path, managed_file=managed_file,
                    supervisor_path=supervisor_path) + "\n")
        return 0

    status, detail, rb_failures = apply(
        DockerOps(), FsOps(), SystemdOps(), unit_path, managed_file,
        supervisor_path)
    if status == 0:
        sys.stdout.write("install_guarded APPLY OK: %s\n" % detail)
        return 0
    if status == 1:
        sys.stderr.write("install_guarded APPLY FAIL (rolled back): %s\n"
                         % detail)
        return 1
    sys.stderr.write("install_guarded APPLY FAIL + ROLLBACK FAIL (status 2): "
                     "%s\n" % detail)
    return 2


if __name__ == "__main__":
    sys.exit(main())

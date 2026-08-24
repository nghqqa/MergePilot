"""Minimal MergePilot CLI tests — production-path, fully mocked.

Everything here drives ``tools.cli.mergepilot.main`` (the console-script
entry) against a FakeWorld that mocks wsl.exe / Docker / HTTP / sockets.
No real WSL distro, Docker daemon, PostgreSQL, or network is ever touched.

Covers: exit-code matrix, install/start/status/stop/cleanup happy paths,
dry-run zero side effects, argv-for-argv equality with the versioned
planner, manifest atomicity + secret-free fields, write-ahead journal,
reverse rollback (and rollback-failure -> 9), name/ID ownership refusal,
orphan-stack fail-closed, idempotent stop/cleanup, never-implicitly-start
a Stopped distro, secret-leak scanning over argv/stdout/JSON, preflight
exited(0)+PREFLIGHT_OK as the healthy terminal state, console-edge
publication-before-connect ordering, and absent/partial/healthy status.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import contextlib
from pathlib import Path
from unittest import mock

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
_ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(_ROOT), str(_ROOT / "tools" / "demo_console"),
           str(_ROOT / "tools" / "cli")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import one_click_startup as oc  # noqa: E402
from tools.cli import mergepilot as mp  # noqa: E402

RUN_ID = "run-showcase-a"
BRIDGE_IP = "172.18.0.2"
IMG_BASE = "sha256:" + "bb" * 32
IMG_PG = "sha256:" + "cc" * 32
IMG_BUILT = {svc: "sha256:" + ("%02x" % i) * 32
             for i, svc in enumerate(oc.BUILT_SERVICES)}
PG_NAME = "mergepilot-isolated-postgres-1"
PREFLIGHT_NAME = "mergepilot-isolated-preflight-1"
EDGE_NAME = "mergepilot-isolated-console-edge-1"
CONTAINER_NAMES = {svc: "mergepilot-isolated-%s-1" % svc
                   for svc in oc.SERVICE_ORDER}
PREFLIGHT_LOGS = '{"ok": "true"}\nPREFLIGHT_OK\n'

_WRITE_CMDS = ("run", "build", "rm", "rmi", "exec")


def _is_write_call(docker_args):
    cmd = docker_args[0]
    if cmd in _WRITE_CMDS:
        return True
    if cmd == "network" and docker_args[1] in ("create", "connect", "rm"):
        return True
    return False


def _docker_of(argv):
    """Extract the docker sub-args from a full wsl.exe argv (or None)."""
    if "docker" not in argv:
        return None
    tail = argv[argv.index("--") + 1:]
    if not tail or tail[0] != "docker":
        return None
    return tail[1:]


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeWorld:
    """Scripted wsl.exe/Docker responder with a mutable resource world."""

    def __init__(self, *, distro_state="Running", endpoint=None,
                 docker_host=None):
        self.calls = []            # {"argv": tuple, "input": bytes|None}
        self.psql_inputs = []
        self.fail_rules = []       # (predicate(argv), FakeProc|Exception)
        self.sticky = False        # rm/rmi return rc=0 but keep the resource
        self.before_write = None   # callback(docker_args) before a write
        self.on_container = None   # callback(name) after a container is run
        self.distro_state = distro_state   # MUTABLE mid-run (simulates wsl
        self.distro_raw = None     # optional raw `wsl -l -v` text override
        self.endpoint = endpoint or mp.APPROVED_ENDPOINT
        self.docker_host = docker_host
        self.info_text = (
            "Client: Docker Engine\nServer:\n"
            " Server Version: 24.0.7\n"
            " Storage Driver: overlay2\n"
            " Docker Root Dir: /var/lib/docker\n"
            " Server ID: 9f8c7b6a5e4d3210\n")
        self.images = {mp.BUILT_BASE_IMAGE: IMG_BASE,
                       oc.PGVECTOR_IMAGE_DIGEST: IMG_PG}
        self.networks = {}
        self.containers = {}
        self.logs_text = {PREFLIGHT_NAME: PREFLIGHT_LOGS}
        self._id_counter = 0

    # -- plumbing ------------------------------------------------------------

    def _next_hex(self):
        self._id_counter += 1
        return "%064x" % self._id_counter

    def run(self, argv, **kw):
        argv = list(argv)
        self.calls.append({"argv": tuple(argv), "input": kw.get("input")})
        for pred, resp in self.fail_rules:
            if pred(argv):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        if argv[0] != "wsl.exe":
            return FakeProc(127, stderr=b"unexpected non-wsl command")
        tail = argv[1:]
        if tail[0] == "-l":
            # The table is built from the CURRENT distro_state on every
            # probe, so a test can flip the simulated distro mid-run and the
            # very next read-only `wsl -l -v` observes it.
            if self.distro_raw is not None:
                return FakeProc(0, stdout=self.distro_raw.encode())
            table = (
                "  NAME            STATE     VERSION\n"
                "* %s    %s   2\n"
                "  Ubuntu-22.04    Stopped   2\n"
                % (mp.AUTHORIZED_DISTRO, self.distro_state))
            return FakeProc(0, stdout=table.encode())
        if tail[0] == "-u":
            after = tail[tail.index("--") + 1:]
            if after[0] == "bash":
                return FakeProc(0, stdout=((self.docker_host or "") + "\n")
                                .encode())
            if after[0] == "docker":
                return self._docker(after[1:], kw.get("input"))
        return FakeProc(125, stderr=b"unrouted wsl command")

    def _docker(self, args, input_bytes):
        if _is_write_call(args) and self.before_write is not None:
            self.before_write(args)
        cmd = args[0]
        if cmd == "context":
            return FakeProc(0, stdout=(self.endpoint + "\n").encode())
        if cmd == "info":
            return FakeProc(0, stdout=self.info_text.encode())
        if cmd == "image" and args[1] == "inspect":
            ref = args[2]
            if ref in self.images:
                return FakeProc(0, stdout=(self.images[ref] + "\n").encode())
            return FakeProc(1, stderr=("Error: No such image: %s"
                                       % ref).encode())
        if cmd == "inspect":
            target, fmt = args[1], args[args.index("--format") + 1]
            cont = self.containers.get(target)
            if cont is not None:
                if fmt == "{{.Id}}":
                    return FakeProc(0, stdout=(cont["id"] + "\n").encode())
                if fmt.startswith("{{.Id}}@@{{json .State}}"):
                    import json as _json
                    state = {"Status": cont["status"],
                             "ExitCode": int(cont.get("exit_code", "0"))}
                    if cont.get("health"):
                        state["Health"] = {"Status": cont["health"]}
                    line = "%s@@%s" % (cont["id"], _json.dumps(state))
                    return FakeProc(0, stdout=(line + "\n").encode())
                if "IPAddress" in fmt:
                    return FakeProc(0, stdout=(cont.get("ip", "") + "\n")
                                    .encode())
            if target in self.networks and fmt == "{{.Id}}":
                return FakeProc(0, stdout=(self.networks[target] + "\n")
                                .encode())
            return FakeProc(1, stderr=("Error: No such object: %s"
                                       % target).encode())
        if cmd == "network":
            sub = args[1]
            if sub == "create":
                name = args[-1]
                nid = self._next_hex()
                self.networks[name] = nid
                return FakeProc(0, stdout=(nid + "\n").encode())
            if sub == "connect":
                return FakeProc(0)
            if sub == "rm":
                target = args[2]
                for name, nid in list(self.networks.items()):
                    if name == target or nid == target:
                        if not self.sticky:
                            del self.networks[name]
                        return FakeProc(0, stdout=(name + "\n").encode())
                return FakeProc(1, stderr=b"Error: network not found")
        if cmd == "run":
            name = args[args.index("--name") + 1]
            cid = "sha256:" + self._next_hex()
            record = {"id": cid, "status": "running", "health": "healthy",
                      "exit_code": "0",
                      "ip": BRIDGE_IP if "postgres" in name else ""}
            if name == PREFLIGHT_NAME:
                record.update({"status": "exited", "health": "",
                               "exit_code": "0"})
            self.containers[name] = record
            if self.on_container is not None:
                self.on_container(name)
            return FakeProc(0, stdout=(cid + "\n").encode())
        if cmd == "exec":
            self.psql_inputs.append(input_bytes or b"")
            return FakeProc(0)
        if cmd == "logs":
            name = args[1]
            return FakeProc(0, stdout=self.logs_text.get(name, "")
                            .encode())
        if cmd == "rm":
            target = args[args.index("-fv") + 1]
            if not self.sticky:
                for name, rec in list(self.containers.items()):
                    if name == target or rec["id"] == target:
                        del self.containers[name]
                        break
            return FakeProc(0)
        if cmd == "rmi":
            if not self.sticky:
                for tag in [t for t, i in self.images.items()
                            if i == args[1]]:
                    del self.images[tag]
            return FakeProc(0)
        if cmd == "build":
            tag = args[args.index("-t") + 1]
            self.images.setdefault(tag, "sha256:" + self._next_hex())
            return FakeProc(0)
        return FakeProc(125, stderr=b"unrouted docker command")

    # -- assertions helpers ---------------------------------------------------

    def docker_args(self):
        out = []
        for call in self.calls:
            argv = call["argv"]
            if argv[0] != "wsl.exe":
                continue
            tail = list(argv[1:])
            if tail[0] != "-u":
                continue
            after = tail[tail.index("--") + 1:]
            if after[0] == "docker":
                out.append(after[1:])
        return out

    def write_args(self):
        return [a for a in self.docker_args() if _is_write_call(a)]

    def has_dash_d(self):
        # usability round §2: the bounded WAKE (wsl -d <distro> --exec
        # /bin/true — a distro BOOT, never a docker command) is
        # allowed; every other -d emission stays forbidden.
        return any(
            "-d" in call["argv"] and not (
                list(call["argv"])[1:2] == ["-d"]
                and list(call["argv"])[3:4] == ["--exec"]
                and list(call["argv"])[4:5] == ["/bin/true"])
            for call in self.calls)


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RefusedSocket:
    def __init__(self, *a, **kw):
        pass

    def settimeout(self, *a):
        pass

    def connect(self, *a):
        raise ConnectionRefusedError("refused")

    def close(self):
        pass


class CliTestBase(unittest.TestCase):
    """Common fixture: real repo as project dir, mocked execution plane."""

    def setUp(self):
        super().setUp()
        mp._PLANNER = None
        mp._SHOWCASE = None
        mp._JSON_MODE = False
        oc._builtin_registry.clear()

        self._repo_backup = None
        state_dir = _ROOT / ".mergepilot"
        if state_dir.exists():
            self._repo_backup = Path(tempfile.mkdtemp(
                prefix="mp-test-backup-")) / ".mergepilot"
            shutil.move(str(state_dir), str(self._repo_backup))
        self.addCleanup(self._restore_state_dir)

        self.world = FakeWorld()
        self.generated_secrets = []
        _self = self

        def _fixed_token(_n):
            value = ("fixedsecret%04daaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                     % len(_self.generated_secrets))
            _self.generated_secrets.append(value)
            return value

        patches = [
            mock.patch("tools.cli.mergepilot.subprocess.run", self.world.run),
            mock.patch("tools.cli.mergepilot.secrets.token_urlsafe",
                       side_effect=_fixed_token),
            mock.patch("tools.cli.mergepilot.urllib.request.urlopen",
                       side_effect=self._urlopen),
            mock.patch("tools.cli.mergepilot.socket.socket", _RefusedSocket),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.outputs = []
        self.endpoint_body = {"source_read_only": True,
                              "not_production": True,
                              "production_resource_accessed": None}
        self.endpoint_error = None

    def _restore_state_dir(self):
        state_dir = _ROOT / ".mergepilot"
        if state_dir.exists():
            shutil.rmtree(str(state_dir), ignore_errors=True)
        if self._repo_backup is not None and self._repo_backup.exists():
            shutil.move(str(self._repo_backup), str(_ROOT / ".mergepilot"))

    def _urlopen(self, url, *a, **kw):
        if self.endpoint_error is not None:
            raise self.endpoint_error
        return _FakeResponse(self.endpoint_body)

    # -- helpers --------------------------------------------------------------

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mp.main(list(argv))
        self.outputs.append(out.getvalue())
        self.outputs.append(err.getvalue())
        text = out.getvalue() + err.getvalue()
        try:
            payload = json.loads(out.getvalue()) if "--json" in argv else None
        except ValueError:
            payload = None
        return rc, text, payload

    def write_install_manifest(self):
        images = {mp.image_tag(oc, svc): IMG_BUILT.get(
            svc, "sha256:" + "ab" * 32)
                  for svc in oc.BUILT_SERVICES}
        manifest = {"schema_version": 1, "project_root": str(_ROOT),
                    "images": images}
        path = _ROOT / ".mergepilot" / "install.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        for tag, img_id in images.items():       # keep the world consistent
            self.world.images[tag] = img_id
        return manifest

    def run_full_start(self):
        self.write_install_manifest()
        rc, text, payload = self.cli("start", "--run-id", RUN_ID)
        return rc, text, payload

    def assert_no_secret_leak(self):
        forbidden = list(self.generated_secrets) + [
            "postgresql://mergepilot_reader:",
            "postgresql://snapshot_worker:",
            "PASSWORD '"]
        blobs = list(self.outputs)
        for call in self.world.calls:
            blobs.extend(str(t) for t in call["argv"] if isinstance(t, str))
        for blob in blobs:
            for needle in forbidden:
                self.assertNotIn(needle, blob)
        for call in self.world.calls:
            if b"psql" in b" ".join(t.encode() if isinstance(t, str) else t
                                    for t in call["argv"]):
                continue  # SQL legitimately rides stdin input, not argv
            self.assertIsNone(call["input"])


# ── Packaging / import-boundary contract ─────────────────────────────────────

class TestPackagingContract(unittest.TestCase):

    def test_pyproject_console_script(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib requires Python 3.11+")
        data = tomllib.loads((_ROOT / "pyproject.toml").read_text(
            encoding="utf-8"))
        self.assertEqual(data["project"]["scripts"]["mergepilot"],
                         "tools.cli.mergepilot:main")
        self.assertTrue(callable(mp.main))
        self.assertEqual(data["project"]["requires-python"], ">=3.9")

    def test_production_never_imports_tests(self):
        import re
        source = (_ROOT / "tools" / "cli" / "mergepilot.py").read_text(
            encoding="utf-8")
        modules = re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)",
                             source, re.MULTILINE)
        for module in modules:
            self.assertFalse(module.split(".")[0] == "tests",
                             "production imports tests/: %s" % module)
            self.assertNotIn("ephemeral", module)
        self.assertNotIn("EPHEMERAL_PG_VERIFY", source)
        self.assertNotIn("shell=True", source)

    def test_project_dir_reloads_planner_from_selected_checkout(self):
        planner_source = (_ROOT / "tools" / "demo_console" /
                          "one_click_startup.py")
        showcase_source = (_ROOT / "tools" / "demo_console" /
                           "showcase_cases.py")
        old_modules = {name: sys.modules.get(name)
                       for name in ("one_click_startup", "showcase_cases")}
        old_path = list(sys.path)
        old_state = (mp._PLANNER, mp._SHOWCASE, mp._PLANNER_ROOT)
        try:
            with tempfile.TemporaryDirectory(
                    prefix="mergepilot-alt-checkout-") as raw:
                alternate = Path(raw)
                target = alternate / "tools" / "demo_console"
                target.mkdir(parents=True)
                shutil.copy2(str(planner_source), str(target / planner_source.name))
                shutil.copy2(str(showcase_source), str(target / showcase_source.name))

                mp._PLANNER = None
                mp._SHOWCASE = None
                mp._PLANNER_ROOT = None
                primary, _ = mp._load_planner(_ROOT)
                alternate_planner, _ = mp._load_planner(alternate)
                primary_again, _ = mp._load_planner(_ROOT)

                self.assertEqual(Path(primary.__file__).resolve(),
                                 planner_source.resolve())
                self.assertEqual(
                    Path(alternate_planner.__file__).resolve(),
                    (target / planner_source.name).resolve())
                self.assertEqual(Path(primary_again.__file__).resolve(),
                                 planner_source.resolve())
        finally:
            mp._PLANNER, mp._SHOWCASE, mp._PLANNER_ROOT = old_state
            sys.path[:] = old_path
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_gitignore_covers_state_dir(self):
        rules = (_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".mergepilot/", rules)


# ── Argparse / exit-code matrix ──────────────────────────────────────────────

class TestArgparseMatrix(CliTestBase):

    def test_no_command_is_usage_error(self):
        rc, text, _ = self.cli()
        self.assertEqual(rc, mp.EXIT_USAGE)

    def test_unknown_command_exits_two(self):
        with self.assertRaises(SystemExit) as ctx:
            mp.main(["frobnicate"])
        self.assertEqual(ctx.exception.code, 2)

    def test_start_requires_run_id(self):
        with self.assertRaises(SystemExit) as ctx:
            mp.main(["start"])
        self.assertEqual(ctx.exception.code, 2)

    def test_invalid_run_id_is_usage_error(self):
        rc, text, payload = self.cli("start", "--run-id", "bad id;rm",
                                     "--json")
        self.assertEqual(rc, mp.EXIT_USAGE)
        self.assertEqual(payload["error_code"], "RUN_ID_INVALID")
        self.assertEqual(self.world.write_args(), [])

    def test_missing_project_dir_is_usage_error(self):
        rc, text, payload = self.cli("status", "--project-dir",
                                     "Z:/definitely/not/here", "--json")
        self.assertEqual(rc, mp.EXIT_USAGE)
        self.assertEqual(payload["error_code"], "PROJECT_DIR_INVALID")


# ── Doctor ───────────────────────────────────────────────────────────────────

class TestDoctor(CliTestBase):

    def test_happy_path_all_checks_pass(self):
        self.write_install_manifest()
        rc, text, payload = self.cli("doctor", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertTrue(all(c["ok"] for c in payload["checks"]))
        names = {c["name"] for c in payload["checks"]}
        self.assertTrue({"python_version", "project_layout", "planner_chain",
                         "wsl_present", "distro_state", "daemon_endpoint",
                         "docker_host", "daemon_fingerprint", "base_image",
                         "pgvector_image", "stack_state", "port_8600"}
                        <= names)
        self.assertEqual(payload["resources"]["stack"]["classification"],
                         "absent")

    def test_doctor_is_read_only(self):
        self.write_install_manifest()
        self.cli("doctor")
        self.assertEqual(self.world.write_args(), [])

    def test_stopped_distro_fails_without_issuing_dash_d(self):
        self.world.distro_state = "Stopped"
        import os as _os
        _os.environ["MERGEPILOT_WAKE_TIMEOUT_SECS"] = "0.2"
        try:
            rc, text, payload = self.cli("doctor", "--json")
        finally:
            _os.environ.pop("MERGEPILOT_WAKE_TIMEOUT_SECS", None)
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        codes = {c["code"] for c in payload["checks"]}
        self.assertIn("DOCTOR_DISTRO_STOPPED", codes)
        self.assertFalse(self.world.has_dash_d())

    def test_missing_local_image_fails(self):
        self.write_install_manifest()
        del self.world.images[mp.image_tag(oc, "policy-gateway")]
        rc, text, payload = self.cli("doctor", "--json")
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        codes = {c["code"] for c in payload["checks"]}
        self.assertIn("DOCTOR_IMAGE_NOT_BUILT:policy-gateway", codes)

    def test_pgvector_not_cached_fails(self):
        del self.world.images[oc.PGVECTOR_IMAGE_DIGEST]
        rc, text, payload = self.cli("doctor", "--json")
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        codes = {c["code"] for c in payload["checks"]}
        self.assertIn("DOCTOR_PGVECTOR_NOT_CACHED", codes)

    def test_partial_stack_reported(self):
        self.world.containers[PG_NAME] = {
            "id": "sha256:" + "11" * 32, "status": "running",
            "health": "healthy", "exit_code": "0", "ip": BRIDGE_IP}
        rc, text, payload = self.cli("doctor", "--json")
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        codes = {c["code"] for c in payload["checks"]}
        self.assertIn("DOCTOR_STACK_PARTIAL", codes)

    def test_layout_missing_files(self):
        skeleton = Path(tempfile.mkdtemp(prefix="mp-skel-"))
        self.addCleanup(shutil.rmtree, str(skeleton), True)
        for rel in ("tools/demo_console/one_click_startup.py",
                    "tools/demo_console/showcase_cases.py",
                    "docker-compose.yml",
                    "tools/demo_console/migrations/"
                    "001_environment_identity.sql",
                    "tools/demo_console/migrations/"
                    "002_mergepilot_reader_acl.sql"):
            target = skeleton / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(_ROOT / rel), str(target))
        for f in sorted(set(mp.AUDIT_DB_MIGRATION_CHAIN)):
            target = skeleton / "tools" / "audit-db" / f
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(_ROOT / "tools" / "audit-db" / f), str(target))
        for svc in oc.BUILT_SERVICES:
            if svc == "preflight":
                continue  # one Dockerfile deliberately missing
            (skeleton / ("Dockerfile.%s" % svc)).write_text("FROM x\n",
                                                            encoding="utf-8")
        rc, text, payload = self.cli("doctor", "--json", "--project-dir",
                                     str(skeleton))
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        codes = {c["code"] for c in payload["checks"]}
        self.assertIn("DOCTOR_LAYOUT_MISSING", codes)


# ── Install ──────────────────────────────────────────────────────────────────

class TestInstall(CliTestBase):

    def test_dry_run_zero_side_effects(self):
        rc, text, payload = self.cli("install", "--dry-run", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertEqual(self.world.calls, [])
        self.assertFalse((_ROOT / ".mergepilot").exists())
        expected = [oc.plan_build(s) for s in oc.BUILT_SERVICES]
        self.assertEqual(payload["plans"], expected)

    def test_happy_path_and_idempotency(self):
        rc, text, payload = self.cli("install", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        builds = [a for a in self.world.docker_args() if a[0] == "build"]
        self.assertEqual(len(builds), 8)
        expected = [oc.plan_build(s) for s in oc.BUILT_SERVICES]
        self.assertEqual(builds, expected)
        manifest = json.loads((_ROOT / ".mergepilot" / "install.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest), ["images", "project_root",
                                            "schema_version"])
        self.assertEqual(set(manifest["images"]),
                         {mp.image_tag(oc, s) for s in oc.BUILT_SERVICES})
        rc2, _t2, payload2 = self.cli("install", "--json")
        self.assertEqual(rc2, mp.EXIT_OK)
        manifest2 = json.loads((_ROOT / ".mergepilot" / "install.json")
                               .read_text(encoding="utf-8"))
        self.assertEqual(manifest2, manifest)

    def test_env_gate_blocks_before_any_build(self):
        self.world.distro_state = "Stopped"
        import os as _os
        _os.environ["MERGEPILOT_WAKE_TIMEOUT_SECS"] = "0.2"
        try:
            rc, text, payload = self.cli("install", "--json")
        finally:
            _os.environ.pop("MERGEPILOT_WAKE_TIMEOUT_SECS", None)
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        self.assertEqual([a for a in self.world.docker_args()
                          if a[0] == "build"], [])
        self.assertFalse(self.world.has_dash_d())


# ── Start: dry-run / happy / conflicts / rollback ────────────────────────────

class TestStartDryRun(CliTestBase):

    def test_zero_side_effects_and_planner_equality(self):
        self.write_install_manifest()
        for svc in oc.BUILT_SERVICES:
            oc.record_built_image_identity(svc, IMG_BUILT[svc])
        rc, text, payload = self.cli("start", "--run-id", RUN_ID,
                                     "--dry-run", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertEqual(self.world.write_args(), [])
        state = _ROOT / ".mergepilot"
        self.assertFalse((state / "session.json").exists())
        self.assertFalse((state / "secrets").exists())
        secrets = ("postgres.env", "controller.env", "demo_console.env",
                   "gh_webhook.env")
        wsl_base = mp._to_wsl_path(_ROOT / ".mergepilot" / "secrets")
        expected = [
            oc.plan_network_create(),
            oc.plan_publication_network_create(),
            oc.plan_service_run(
                "postgres", image_ref=oc.PGVECTOR_IMAGE_DIGEST,
                env_file=wsl_base + "/postgres.env"),
            oc.plan_service_run(
                "policy-gateway",
                image_ref=oc.get_built_image_identity("policy-gateway"),
                gateway_env=oc._gateway_environment()),
            oc.plan_service_run(
                "controller",
                image_ref=oc.get_built_image_identity("controller"),
                controller_env=oc._controller_environment(),
                env_file=wsl_base + "/controller.env", m4f_enabled=False),
            oc.plan_gh_webhook_run(
                oc.get_built_image_identity("gh-webhook"),
                env_file=wsl_base + "/gh_webhook.env"),
            oc.plan_gh_webhook_connect_backend(),
            oc.plan_service_run(
                "demo-console",
                image_ref=oc.get_built_image_identity("demo-console"),
                demo_console_env=oc._demo_console_environment(
                    RUN_ID, mp.PLACEHOLDER_BRIDGE_IP),
                reader_dsn_env_file=wsl_base + "/demo_console.env",
                session_public_dir=mp._to_wsl_path(
                    _ROOT / ".mergepilot" / "public")),
            oc.plan_console_edge_run(
                oc.get_built_image_identity("console-edge")),
            oc.plan_console_edge_connect_backend(),
            oc.plan_service_run(
                "preflight",
                image_ref=oc.get_built_image_identity("preflight"),
                declared_pg_image=oc.PGVECTOR_IMAGE_DIGEST,
                reader_dsn_env_file=wsl_base + "/demo_console.env"),
        ]
        self.assertEqual(payload["plans"], expected)
        self.assertEqual(len(secrets), 4)


class TestStartHappyPath(CliTestBase):

    def test_full_start_journal_and_preflight(self):
        journal_seen = {}
        world = self.world

        def before_write(docker_args):
            journal_seen.setdefault(
                tuple(docker_args),
                (_ROOT / ".mergepilot" / "session.json").exists())

        world.before_write = before_write
        rc, text, payload = self.run_full_start()
        self.assertEqual(rc, mp.EXIT_OK)
        # journal existed before the FIRST docker write of any kind
        first_write = next(a for a in world.docker_args()
                           if _is_write_call(a))
        self.assertTrue(journal_seen[tuple(first_write)])

        session = json.loads((_ROOT / ".mergepilot" / "session.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(session["run_id"], RUN_ID)
        self.assertEqual(session["stage"], "complete")
        self.assertEqual(sorted(session["containers"]),
                         sorted(oc.SERVICE_ORDER))
        self.assertEqual(sorted(session["networks"]),
                         sorted([oc.ORCHESTRATOR_NETWORK,
                                 oc.PUBLICATION_NETWORK]))
        for svc, cid in session["containers"].items():
            self.assertEqual(world.containers[CONTAINER_NAMES[svc]]["id"],
                             cid)
        for name, nid in session["networks"].items():
            self.assertEqual(world.networks[name], nid)

        # secret files exist with the contracted keys (values are generated)
        secrets = _ROOT / ".mergepilot" / "secrets"
        pg_env = (secrets / "postgres.env").read_text(encoding="utf-8")
        self.assertIn("POSTGRES_USER=mergepilot", pg_env)
        ctrl_env = (secrets / "controller.env").read_text(encoding="utf-8")
        self.assertIn("PG_PASS=", ctrl_env)
        self.assertIn("ADMIN_PW=", ctrl_env)
        dsn_env = (secrets / "demo_console.env").read_text(encoding="utf-8")
        self.assertTrue(dsn_env.startswith("MERGEPILOT_PG_DSN="))

        # argv ordering: edge created on publication network BEFORE connect
        args = world.docker_args()
        edge_run = next(i for i, a in enumerate(args)
                        if a[0] == "run" and EDGE_NAME in a)
        edge_connect = next(i for i, a in enumerate(args)
                            if a[0] == "network" and a[1] == "connect"
                            and EDGE_NAME in a)
        self.assertLess(edge_run, edge_connect)
        # gh-webhook follows the same publication-first rule
        gh_name = "mergepilot-isolated-gh-webhook-1"
        gh_run = next(i for i, a in enumerate(args)
                      if a[0] == "run" and gh_name in a)
        gh_connect = next(i for i, a in enumerate(args)
                          if a[0] == "network" and a[1] == "connect"
                          and gh_name in a)
        self.assertLess(gh_run, gh_connect)
        edge_argv = args[edge_run]
        self.assertIn(oc.PUBLICATION_NETWORK, edge_argv)
        self.assertNotIn("--network=%s" % oc.ORCHESTRATOR_NETWORK,
                         " ".join(edge_argv))
        # bridge IP measured AFTER postgres run, injected canonically
        pg_run = next(i for i, a in enumerate(args)
                      if a[0] == "run" and PG_NAME in a)
        ip_probe = next(i for i, a in enumerate(args)
                        if a[0] == "inspect" and "IPAddress" in
                        " ".join(a))
        self.assertLess(pg_run, ip_probe)
        console_run = next(a for a in args
                           if a[0] == "run"
                           and "demo-console-1" in " ".join(a))
        self.assertIn(BRIDGE_IP, " ".join(console_run))

        # DB prepare: prerequisite roles, reader role, migrations,
        # gh runtime roles, marker, seed
        inputs = [b.decode("utf-8", "replace")
                  for b in world.psql_inputs]
        self.assertEqual(len(inputs), 1 + len(mp.AUDIT_DB_MIGRATION_CHAIN)
                         + 1 + len(mp.ISOLATED_LIVE_MIGRATIONS) + 1 + 1 + 1)
        self.assertIn("policy_gateway_l2", inputs[0])
        reader_sql = inputs[1 + len(mp.AUDIT_DB_MIGRATION_CHAIN)]
        self.assertIn("CREATE ROLE mergepilot_reader", reader_sql)
        self.assertIn("default_transaction_read_only", reader_sql)
        # M8-GH-3: gh runtime role bootstrap runs AFTER prepare_database
        gh_role_sql = inputs[-1]
        self.assertIn("ALTER ROLE github_event_ingress PASSWORD",
                      gh_role_sql)
        self.assertIn("ALTER ROLE github_check_publisher PASSWORD",
                      gh_role_sql)
        joined = "\n".join(inputs)
        self.assertIn("environment_identity", joined)
        self.assertIn("INSERT INTO task_runs", joined)

        self.assert_no_secret_leak()

    def test_preflight_bad_output_triggers_rollback(self):
        self.write_install_manifest()
        self.world.logs_text[PREFLIGHT_NAME] = '{"ok": "true"}\nNOPE\n'
        rc, text, payload = self.cli("start", "--run-id", RUN_ID)
        self.assertEqual(rc, mp.EXIT_FAILED_CLEANED)
        self.assertIn("PREFLIGHT_OUTPUT_INVALID", text)
        self.assertFalse((_ROOT / ".mergepilot" / "session.json").exists())
        self.assertEqual(self.world.containers, {})
        self.assertEqual(self.world.networks, {})

    def test_idempotent_same_run_id(self):
        rc, _t, payload = self.run_full_start()
        self.assertEqual(rc, mp.EXIT_OK)
        writes_before = len(self.world.write_args())
        rc2, text2, payload2 = self.cli("start", "--run-id", RUN_ID,
                                        "--json")
        self.assertEqual(rc2, mp.EXIT_OK)
        self.assertTrue(payload2["idempotent"])
        self.assertEqual(len(self.world.write_args()), writes_before)

    def test_run_id_mismatch_conflict(self):
        self.run_full_start()
        rc, text, payload = self.cli("start", "--run-id", "run-showcase-b",
                                     "--json")
        self.assertEqual(rc, mp.EXIT_CONFLICT)
        self.assertEqual(payload["error_code"], "RUN_ID_MISMATCH")


class TestStartConflicts(CliTestBase):

    def test_orphan_stack_without_session(self):
        self.write_install_manifest()
        self.world.containers[PG_NAME] = {
            "id": "sha256:" + "22" * 32, "status": "running",
            "health": "healthy", "exit_code": "0", "ip": BRIDGE_IP}
        rc, text, payload = self.cli("start", "--run-id", RUN_ID, "--json")
        self.assertEqual(rc, mp.EXIT_CONFLICT)
        self.assertEqual(payload["error_code"], "ORPHAN_STACK")
        self.assertEqual(self.world.write_args(), [])

    def test_partial_stack_conflict(self):
        self.write_install_manifest()
        session = mp.new_session(RUN_ID, False)
        session["stage"] = "services"
        session["containers"]["postgres"] = "sha256:" + "33" * 32
        session["networks"][oc.ORCHESTRATOR_NETWORK] = "44" * 32
        session["networks"][oc.PUBLICATION_NETWORK] = "55" * 32
        path = _ROOT / ".mergepilot" / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session), encoding="utf-8")
        self.world.containers[PG_NAME] = {
            "id": "sha256:" + "33" * 32, "status": "running",
            "health": "healthy", "exit_code": "0", "ip": BRIDGE_IP}
        self.world.networks[oc.ORCHESTRATOR_NETWORK] = "44" * 32
        self.world.networks[oc.PUBLICATION_NETWORK] = "55" * 32
        rc, text, payload = self.cli("start", "--run-id", RUN_ID, "--json")
        self.assertEqual(rc, mp.EXIT_CONFLICT)
        self.assertEqual(payload["error_code"], "STACK_PARTIAL")
        self.assertEqual(self.world.write_args(), [])

    def test_secret_residue_without_session(self):
        self.write_install_manifest()
        secrets = _ROOT / ".mergepilot" / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / "postgres.env").write_text("POSTGRES_PASSWORD=x\n",
                                              encoding="utf-8")
        rc, text, payload = self.cli("start", "--run-id", RUN_ID, "--json")
        self.assertEqual(rc, mp.EXIT_CONFLICT)
        self.assertEqual(payload["error_code"], "SECRET_RESIDUE")

    def test_not_installed(self):
        rc, text, payload = self.cli("start", "--run-id", RUN_ID, "--json")
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        self.assertEqual(payload["error_code"], "NOT_INSTALLED")
        self.assertEqual(self.world.write_args(), [])

    def test_stopped_distro_wakes_bounded_or_times_out(self):
        # usability round §2: operator lifecycle commands BOUNDED-WAKE
        # a registered-but-dormant distro. In this fake world the wake
        # cannot bring it up, so the stable outcome is
        # DISTRO_WAKE_TIMEOUT — and crucially no docker -d emission
        # and no writes ever happen on the failed path.
        self.write_install_manifest()
        self.world.distro_state = "Stopped"
        import os
        os.environ["MERGEPILOT_WAKE_TIMEOUT_SECS"] = "0.2"
        try:
            rc, text, payload = self.cli("start", "--run-id", RUN_ID,
                                         "--json")
        finally:
            os.environ.pop("MERGEPILOT_WAKE_TIMEOUT_SECS", None)
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        self.assertEqual(payload.get("error_code"), "DISTRO_WAKE_TIMEOUT")
        self.assertFalse(self.world.has_dash_d())
        self.assertEqual(self.world.write_args(), [])


class TestStartRollback(CliTestBase):

    def _fail_controller_run(self):
        self.write_install_manifest()

        def is_controller_run(argv):
            d = _docker_of(list(argv))
            return bool(d and d[0] == "run"
                        and "mergepilot-isolated-controller-1" in d)

        self.world.fail_rules.append(
            (is_controller_run, FakeProc(125, stderr=b"docker: conflict")))

    def test_mid_failure_reverse_rollback_exit_five(self):
        self._fail_controller_run()
        rc, text, payload = self.cli("start", "--run-id", RUN_ID, "--json")
        self.assertEqual(rc, mp.EXIT_FAILED_CLEANED)
        self.assertEqual(payload["primary_code"], "DOCKER_FAILED")
        self.assertEqual(payload["rollback_codes"], [])
        args = self.world.docker_args()
        rms = [i for i, a in enumerate(args) if a[0] == "rm"]
        net_rms = [i for i, a in enumerate(args)
                   if a[0] == "network" and a[1] == "rm"]
        self.assertTrue(rms)
        self.assertTrue(net_rms)
        self.assertLess(max(rms), min(net_rms))   # containers before networks
        self.assertEqual(self.world.containers, {})
        self.assertEqual(self.world.networks, {})
        secrets = _ROOT / ".mergepilot" / "secrets"
        self.assertFalse(any(secrets.glob("*.env")))
        self.assertFalse((_ROOT / ".mergepilot" / "session.json").exists())
        install_kept = (_ROOT / ".mergepilot" / "install.json").exists()
        self.assertTrue(install_kept)
        self.assert_no_secret_leak()

    def test_psql_gate_reprobes_distro_and_never_emits_psql_dash_d(self):
        """Mid-run distro shutdown: psql_exec re-probes `wsl -l -v`, never
        emits the `-d ... docker exec ... psql` command, start fails into
        the rollback path, and a rollback that cannot run reports residue 9
        with the journal retained (no faked rollback success)."""
        self.write_install_manifest()
        world = self.world

        def flip_distro_stopped_after_postgres(name):
            if name == PG_NAME:
                world.distro_state = "Stopped"

        world.on_container = flip_distro_stopped_after_postgres
        rc, text, payload = self.cli("start", "--run-id", RUN_ID, "--json")
        self.assertEqual(rc, mp.EXIT_RESIDUE)
        self.assertEqual(payload["primary_code"], "DISTRO_NOT_RUNNING")
        self.assertTrue(payload["rollback_codes"])

        # the psql gate re-executed the read-only `wsl -l -v` AFTER the flip
        wsl_list_calls = [c for c in world.calls if c["argv"][1:2] == ("-l",)]
        self.assertEqual(len(wsl_list_calls), 2)
        # NO wsl ... -d ... docker exec ... psql was ever constructed/issued
        self.assertEqual([c for c in world.calls if "psql" in c["argv"]], [])
        self.assertEqual(world.psql_inputs, [])
        # honest residue: journal kept, created resources kept
        self.assertTrue((_ROOT / ".mergepilot" / "session.json").exists())
        self.assertIn(PG_NAME, world.containers)
        self.assertEqual(len(world.networks), 2)

    def test_rollback_failure_exit_nine(self):
        self._fail_controller_run()
        world = self.world
        state = {"hit": False}

        def pg_rm_fails_once(argv):
            d = _docker_of(list(argv))
            if not (d and d[0] == "rm"):
                return False
            rec = world.containers.get(PG_NAME)
            if PG_NAME in d or (rec and rec["id"] in d):
                if not state["hit"]:
                    state["hit"] = True
                    return True
            return False

        world.fail_rules.append(
            (pg_rm_fails_once, FakeProc(1, stderr=b"docker: rm failed")))
        rc, text, payload = self.cli("start", "--run-id", RUN_ID, "--json")
        self.assertEqual(rc, mp.EXIT_RESIDUE)
        self.assertEqual(payload["primary_code"], "DOCKER_FAILED")
        self.assertTrue(payload["rollback_codes"])
        # rolled-back container is still there (rm failed) — residue honest
        self.assertIn(PG_NAME, world.containers)
        self.assertTrue((_ROOT / ".mergepilot" / "session.json").exists())


# ── Status ───────────────────────────────────────────────────────────────────

class TestStatus(CliTestBase):

    def test_absent(self):
        rc, text, payload = self.cli("status", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertEqual(payload["status"], "absent")
        self.assertEqual(payload["command"], "status")
        self.assertIn("resources", payload)

    def test_partial(self):
        self.world.containers[PG_NAME] = {
            "id": "sha256:" + "66" * 32, "status": "running",
            "health": "healthy", "exit_code": "0", "ip": BRIDGE_IP}
        rc, text, payload = self.cli("status", "--json")
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        self.assertEqual(payload["status"], "partial")

    def test_healthy_after_start(self):
        self.run_full_start()
        rc, text, payload = self.cli("status", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["session"]["run_id"], RUN_ID)
        self.assert_no_secret_leak()

    def test_preflight_nonzero_exit_is_not_healthy(self):
        self.run_full_start()
        self.world.containers[PREFLIGHT_NAME]["exit_code"] = "1"
        rc, text, payload = self.cli("status", "--json")
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        self.assertEqual(payload["status"], "partial")

    def test_endpoint_unreachable_is_not_healthy(self):
        self.run_full_start()
        self.endpoint_error = OSError("down")
        rc, text, payload = self.cli("status", "--json")
        self.assertEqual(rc, mp.EXIT_PRECHECK)
        self.assertEqual(payload["status"], "partial")

    def test_orphan_resources_flagged(self):
        self.world.containers[PG_NAME] = {
            "id": "sha256:" + "77" * 32, "status": "running",
            "health": "healthy", "exit_code": "0", "ip": BRIDGE_IP}
        rc, text, payload = self.cli("status", "--json")
        self.assertIn("conflict", payload.get("ownership", ""))


# ── Stop ─────────────────────────────────────────────────────────────────────

class TestStop(CliTestBase):

    def test_happy_path_matches_planner_and_keeps_install(self):
        self.run_full_start()
        rc, text, payload = self.cli("stop", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        args = self.world.docker_args()
        executed = [a for a in args if (a[0] == "rm"
                                        or (a[0] == "network"
                                            and a[1] == "rm"))]
        self.assertEqual(executed, oc.plan_orchestrated_cleanup())
        self.assertEqual(self.world.containers, {})
        self.assertEqual(self.world.networks, {})
        secrets = _ROOT / ".mergepilot" / "secrets"
        self.assertFalse(any(secrets.glob("*.env")))
        self.assertFalse((_ROOT / ".mergepilot" / "session.json").exists())
        self.assertTrue((_ROOT / ".mergepilot" / "install.json").exists())
        self.assertTrue(all(mp.image_tag(oc, s) in self.world.images
                            for s in oc.BUILT_SERVICES))

    def test_idempotent_second_stop(self):
        self.run_full_start()
        self.cli("stop")
        writes = len(self.world.write_args())
        rc, text, payload = self.cli("stop", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertTrue(payload["idempotent"])
        self.assertEqual(len(self.world.write_args()), writes)

    def test_dry_run_zero_writes(self):
        self.run_full_start()
        writes = len(self.world.write_args())
        rc, text, payload = self.cli("stop", "--dry-run", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertEqual(len(self.world.write_args()), writes)
        self.assertEqual(payload["plans"], oc.plan_orchestrated_cleanup())
        self.assertTrue(self.world.containers)

    def test_name_present_id_different_refuses_delete(self):
        self.run_full_start()
        # preflight is FIRST in the reverse cleanup order — a mismatch here
        # must abort before ANY rm is executed.
        self.world.containers[PREFLIGHT_NAME]["id"] = "sha256:" + "ee" * 32
        rc, text, payload = self.cli("stop", "--json")
        self.assertEqual(rc, mp.EXIT_CONFLICT)
        self.assertEqual(payload["error_code"], "OWNERSHIP_MISMATCH")
        self.assertEqual([a for a in self.world.docker_args()
                          if a[0] == "rm"], [])
        self.assertEqual(len(self.world.containers), 7)

    def test_orphan_stack_refuses_stop(self):
        self.world.containers[PG_NAME] = {
            "id": "sha256:" + "88" * 32, "status": "running",
            "health": "healthy", "exit_code": "0", "ip": BRIDGE_IP}
        rc, text, payload = self.cli("stop", "--json")
        self.assertEqual(rc, mp.EXIT_CONFLICT)
        self.assertEqual(payload["error_code"], "ORPHAN_STACK")
        self.assertEqual(self.world.write_args(), [])

    def test_daemon_error_is_not_absent(self):
        self.run_full_start()

        def bad_inspect(argv):
            d = _docker_of(list(argv))
            return bool(d and d[0] == "inspect" and PG_NAME in d)

        self.world.fail_rules.append(
            (bad_inspect, FakeProc(1, stderr=b"daemon unreachable")))
        rc, text, payload = self.cli("stop", "--json")
        self.assertEqual(rc, mp.EXIT_FAILED_CLEANED)
        self.assertEqual(payload["error_code"], "DOCKER_INSPECT_FAILED")
        self.assertEqual([a for a in self.world.docker_args()
                          if a[0] == "rm"], [])

    def test_residue_after_rm_returns_nine(self):
        self.run_full_start()
        self.world.sticky = True
        rc, text, payload = self.cli("stop", "--json")
        self.assertEqual(rc, mp.EXIT_RESIDUE)
        self.assertTrue(payload["residue_codes"])
        self.assertEqual(len(self.world.containers), 7)


# ── Cleanup ──────────────────────────────────────────────────────────────────

class TestCleanup(CliTestBase):

    def test_default_is_dry_run(self):
        self.run_full_start()
        writes = len(self.world.write_args())
        rc, text, payload = self.cli("cleanup", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        self.assertEqual(payload["status"], "dry-run")
        self.assertEqual(len(self.world.write_args()), writes)
        self.assertTrue(self.world.containers)
        plans = payload["plans"]
        self.assertEqual(len([p for p in plans if p[0] == "rmi"]), 8)

    def test_apply_removes_verified_images(self):
        self.run_full_start()
        rc, text, payload = self.cli("cleanup", "--apply", "--json")
        self.assertEqual(rc, mp.EXIT_OK)
        rmis = [a for a in self.world.docker_args() if a[0] == "rmi"]
        self.assertEqual(sorted(rmis),
                         sorted([["rmi", IMG_BUILT[s]]
                                 for s in oc.BUILT_SERVICES]))
        self.assertFalse((_ROOT / ".mergepilot" / "install.json").exists())
        self.assertFalse((_ROOT / ".mergepilot" / "session.json").exists())
        for svc in oc.BUILT_SERVICES:
            self.assertNotIn(mp.image_tag(oc, svc), self.world.images)

    def test_apply_idempotent(self):
        self.run_full_start()
        self.cli("cleanup", "--apply")
        rc, text, payload = self.cli("cleanup", "--apply", "--json")
        self.assertEqual(rc, mp.EXIT_OK)

    def test_image_id_mismatch_refuses_rmi(self):
        self.run_full_start()
        self.world.images[mp.image_tag(oc, "controller")] = \
            "sha256:" + "dd" * 32
        rc, text, payload = self.cli("cleanup", "--apply", "--json")
        self.assertEqual(rc, mp.EXIT_CONFLICT)
        # the MISMATCHED image is never deleted (earlier verified ones may
        # already be gone — cleanup iterates in sorted-tag order and aborts
        # at the first conflict)
        ctrl_rmi = [a for a in self.world.docker_args()
                    if a[0] == "rmi" and IMG_BUILT["controller"] in a]
        self.assertEqual(ctrl_rmi, [])
        self.assertIn(mp.image_tag(oc, "controller"), self.world.images)

    def test_image_residue_returns_nine(self):
        self.run_full_start()
        self.world.sticky = True
        rc, text, payload = self.cli("cleanup", "--apply", "--json")
        self.assertEqual(rc, mp.EXIT_RESIDUE)
        self.assertTrue(payload["residue_codes"])


# ── Manifest hygiene / atomicity / secrets ───────────────────────────────────

class TestManifestHygiene(CliTestBase):

    def test_manifests_atomic_secret_free_no_tmp_residue(self):
        self.run_full_start()
        state = _ROOT / ".mergepilot"
        leftovers = [p.name for p in state.iterdir()
                     if p.is_file() and p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        session_text = (state / "session.json").read_text(encoding="utf-8")
        install_text = (state / "install.json").read_text(encoding="utf-8")
        session = json.loads(session_text)
        self.assertEqual(sorted(session),
                         ["containers", "created_utc", "m4f", "networks",
                          "run_id", "schema_version", "secrets", "stage"])
        for needle in (self.generated_secrets
                       + ["postgresql://", "PASSWORD ", "PG_PASS="]):
            self.assertNotIn(needle, session_text)
            self.assertNotIn(needle, install_text)
        # secret VALUES live only in the 0600 env files, never in manifests
        secrets_dir = state / "secrets"
        for pw in self.generated_secrets:
            hits = [p.name for p in secrets_dir.glob("*.env")
                    if pw in p.read_text(encoding="utf-8")]
            self.assertTrue(hits)

    def test_json_outputs_stable_shape(self):
        self.run_full_start()
        rc, text, payload = self.cli("status", "--json")
        for key in ("command", "status", "code", "resources"):
            self.assertIn(key, payload)
        rc2, text2, doctor_payload = self.cli("doctor", "--json")
        for key in ("command", "status", "code", "checks", "resources"):
            self.assertIn(key, doctor_payload)
        self.assert_no_secret_leak()


# ── wsl -l -v parsing ────────────────────────────────────────────────────────

class TestWslParsing(unittest.TestCase):

    def test_utf16_style_nul_output_and_default_marker(self):
        world = FakeWorld()
        # wsl.exe may emit UTF-16LE-flavored bytes; decoded loosely this
        # yields NUL-separated text. The parser must strip NULs, drop the
        # default-distro '*' marker, and still classify both rows.
        world.distro_raw = (
            "*\x00 \x00M\x00e\x00r\x00g\x00e\x00P\x00i\x00l\x00o\x00t\x00"
            "-\x00T\x00e\x00s\x00t\x00 \x00 \x00 \x00R\x00u\x00n\x00n\x00"
            "i\x00n\x00g\x00 \x00 \x00 2\x00\n\x00"
            " \x00 \x00U\x00b\x00u\x00n\x00t\x00u\x00-\x002\x002\x00.\x00"
            "0\x004\x00 \x00 \x00 \x00S\x00t\x00o\x00p\x00p\x00e\x00d\x00"
            " \x00 \x00 2\x00\n\x00")
        docker = mp.WslDocker(oc, _ROOT)
        with mock.patch("tools.cli.mergepilot.subprocess.run", world.run):
            states = docker.distro_states()
        self.assertEqual(states.get("MergePilot-Test"), "Running")
        self.assertEqual(states.get("Ubuntu-22.04"), "Stopped")

    def test_psql_exec_stopped_distro_never_emits_dash_d(self):
        """Direct helper contract: with the distro Stopped, psql_exec probes
        `wsl -l -v` read-only and refuses BEFORE constructing any `-d`
        command (which would implicitly start the distro)."""
        world = FakeWorld(distro_state="Stopped")
        docker = mp.WslDocker(oc, _ROOT)
        with mock.patch("tools.cli.mergepilot.subprocess.run", world.run):
            with self.assertRaises(mp.Failure) as ctx:
                docker.psql_exec(PG_NAME, "SELECT 1;")
        self.assertEqual(ctx.exception.code, "DISTRO_NOT_RUNNING")
        self.assertFalse(any("-d" in c["argv"] for c in world.calls))
        self.assertEqual([c for c in world.calls if "psql" in c["argv"]], [])
        wsl_list_calls = [c for c in world.calls if c["argv"][1:2] == ("-l",)]
        self.assertEqual(len(wsl_list_calls), 1)


if __name__ == "__main__":
    unittest.main()

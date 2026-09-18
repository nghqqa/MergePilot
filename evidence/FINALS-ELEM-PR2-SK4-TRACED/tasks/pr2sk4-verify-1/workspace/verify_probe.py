"""Independent verification probe for pr2sk4-verify-1.

Loads the REAL demo_high_risk module from the cloned repo via importlib and
mounts it on a fresh FastAPI app (TestClient). Uses throwaway temp dirs and
monkeypatches DEMO_FILES_DIR at runtime — lives OUTSIDE the repo, modifies NO
repository test. This direct-import approach also bypasses the repo conftest
(which needs Google OAuth env values), per the spec's environment note.

Usage:
    REPO_ROOT=<clone> /opt/venv/standard/bin/python verify_probe.py
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = os.environ.get("REPO_ROOT", "").strip()
if not REPO_ROOT:
    print("ERROR: set REPO_ROOT")
    sys.exit(2)

MODULE_PATH = Path(REPO_ROOT) / "backend/src/interfaces/api/v1/demo_high_risk.py"
assert MODULE_PATH.is_file(), f"module not found: {MODULE_PATH}"


def load_module():
    spec = importlib.util.spec_from_file_location("demo_high_risk_probe", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_client(module):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(module.router, prefix="/demo-security")
    return TestClient(app, raise_server_exceptions=False)


def main():
    module = load_module()
    client = make_client(module)

    tmp = tempfile.mkdtemp(prefix="verify-probe-")
    root = Path(tmp)
    base = root / "demo-files"
    base.mkdir(parents=True)
    (base / "ok.txt").write_text("LEGIT-INSIDE-BASE", encoding="utf-8")
    (base / "sub").mkdir()
    outside = root / "outside"
    outside.mkdir()
    (outside / "outside-secret.txt").write_text("TOP-SECRET-OUTSIDE-BASE", encoding="utf-8")
    (root / "deep-secret.txt").write_text("DEEP-SECRET", encoding="utf-8")

    module.DEMO_FILES_DIR = str(base)

    try:
        hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
    except Exception as e:  # pragma: no cover
        hostname = f"<unreadable: {e}>"

    print(f"[i] probe tmp root      : {tmp}")
    print(f"[i] base dir (served)   : {base}")
    print(f"[i] /etc/hostname       : {hostname!r}")
    print()

    cases = [
        ("legit in-base file",         "ok.txt",                          "legit"),
        ("traversal ../",              "../outside/outside-secret.txt",   "escape"),
        ("nested ../ via sub dir",     "sub/../../outside/outside-secret.txt", "escape"),
        ("absolute-target traversal",  "../../../../../../etc/hostname",  "escape"),
        ("missing in-base file",       "does-not-exist.txt",              "missing"),
    ]

    results = []
    for label, name, kind in cases:
        r = client.get("/demo-security/demo/download", params={"name": name})
        body = r.text
        short = body if len(body) <= 60 else body[:57] + "..."
        leak = (
            ("TOP-SECRET-OUTSIDE-BASE" in body)
            or ("DEEP-SECRET" in body)
            or (hostname and hostname in body)
        )
        if kind == "legit":
            ok = (r.status_code == 200) and ("LEGIT-INSIDE-BASE" in body)
            exp = "200 + LEGIT content"
        elif kind == "escape":
            ok = (r.status_code in (400, 404)) and not leak
            exp = "400/404, no out-of-base content"
        else:
            ok = (r.status_code == 404)
            exp = "404"
        results.append(ok)
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {label:26s} name={name!r}")
        print(f"      status={r.status_code} (expected: {exp})  body={short!r}")
        if leak:
            print("      !! LEAK: out-of-base content returned")
        print()

    print("PROBE_ACCEPTANCE:", "PASS" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

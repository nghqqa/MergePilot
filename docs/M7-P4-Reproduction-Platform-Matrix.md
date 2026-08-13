# M7-P4 Reproduction Platform Matrix (v3 corrected)

**Status**: Design only

## Environment Matrix

| Dimension | Primary | Backup | Failure Backup |
|-----------|---------|--------|----------------|
| **OS** | Windows 10/11 (PowerShell) | MergePilot-Test WSL (bash) | Any OS with Python + Git |
| **Python** | 3.9.x (planned, PLANNED_NOT_YET_CLEAN_VERIFIED) | 3.10.x (candidate) | Other (NOT_YET_VERIFIED) |
| **Git CLI** | Required | Required | Required |
| **Browser** | Chrome/Edge | Firefox | Direct file open |
| **HTTP server** | `python -m http.server --bind 127.0.0.1` | `serve.py` | Direct file open |
| **Network (runtime)** | Not required | Not required | Not required |
| **Network (clone)** | Required (HTTPS) | Not required (bundle) | N/A |
| **Docker** | Not needed | Not needed | Not needed |
| **PostgreSQL** | Not needed | Not needed | Not needed |
| **LLM API** | Not needed | Not needed | Not needed |
| **SLS** | Not needed | Not needed | Not needed |
| **GitHub (runtime)** | Not needed | Not needed | Not needed |

## System Tool Dependencies

| Tool | Required by | Cannot omit |
|------|------------|------------|
| Python interpreter | All modules (schema/builder/render/serve/tests) | Yes |
| Git CLI | `bundle_builder.py` (calls `git rev-parse HEAD`), unittest suite (verifies git blob SHAs), source checkout | Yes |
| Browser | Page display only | No (verification works without browser) |

**Can claim**: "Zero third-party Python packages; no pip install needed."
**Cannot claim**: "Zero external dependencies" or "Only Python needed."

## Python Version Status

| Version | Status | Evidence |
|---------|--------|----------|
| 3.9.25 (Windows) | `HISTORICALLY_EXERCISED` | Demo Console tests ran on 3.9.25 in development |
| 3.10.x | NOT listed | No Demo Console test run on 3.10 is traceable |
| 3.8.x | `NOT_YET_VERIFIED` | Candidate only — f-strings supported but untested |

| Field | Value |
|-------|-------|
| `candidate_minimum_python_version` | `"3.8"` |
| `minimum_python_version_status` | `"NOT_YET_VERIFIED"` |
| `historically_exercised_python_versions` | `["3.9"]` |
| `clean_reproduction_verified_python_versions` | `[]` (empty) |
| `planned_primary_python_version` | `"3.9"` |
| `planned_primary_status` | `"PLANNED_NOT_YET_CLEAN_VERIFIED"` |

Only after running the full unittest suite in a clean reproduction on a
specific Python version may that version be added to
`clean_reproduction_verified_python_versions`.

## Dependency Analysis

| Module | Python imports | Third-party? | Needs Git? |
|--------|---------------|-------------|------------|
| `schema.py` | json, re | No | No |
| `bundle_builder.py` | json, hashlib, os, re, sys, subprocess, time, pathlib | No | **Yes** (`subprocess` → `git rev-parse HEAD`) |
| `render.py` | json, os, sys, pathlib, re | No | No |
| `serve.py` | argparse, http.server, os, socketserver, sys, pathlib | No | No |
| `test_demo_console.py` | json, hashlib, os, re, sys, unittest, pathlib, tempfile, subprocess | No | **Yes** (tests use `git cat-file`, `git diff`, `git status`) |

**Conclusion**:
- `python_package_dependencies = []`
- `pip_install_required = false`
- `system_tool_dependencies = ["python", "git"]`
- NOT "zero external dependencies" — Git CLI is required.

## Source Acquisition

### GitHub HTTPS Acquisition

Network used for source fetch only. Subsequent replay and tests are offline.

```bash
# Set the reproduction spec commit
REPRO_SPEC_COMMIT="REPLACE_WITH_FULL_MERGE_COMMIT_SHA"

git clone https://github.com/nghqqa/MergePilot.git .
git checkout "$REPRO_SPEC_COMMIT"
```

Result: `source_acquisition_offline = false`.

### POSIX Bundle Preparation

On a networked machine, create and verify a git bundle from the named ref
`origin/main`. A bare SHA produces `fatal: Refusing to create empty bundle`;
always use the named ref.

```bash
# Set the reproduction spec commit
REPRO_SPEC_COMMIT="REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
SOURCE_REF="refs/remotes/origin/main"
BUNDLE_PATH="mergepilot-${REPRO_SPEC_COMMIT}.bundle"

git fetch origin main
ACTUAL_MAIN="$(git rev-parse "$SOURCE_REF")"
test "$ACTUAL_MAIN" = "$REPRO_SPEC_COMMIT"

git bundle create "$BUNDLE_PATH" "$SOURCE_REF"
git bundle verify "$BUNDLE_PATH"
git bundle list-heads "$BUNDLE_PATH"
sha256sum "$BUNDLE_PATH"
```

### PowerShell Bundle Preparation

On a networked Windows machine, create and verify a git bundle from
`origin/main`.

```powershell
# Set the reproduction spec commit
$ReproSpecCommit = "REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
$SourceRef = "refs/remotes/origin/main"
$BundlePath = "mergepilot-$ReproSpecCommit.bundle"

git fetch origin main
$ActualMain = (git rev-parse $SourceRef).Trim()
if ($ActualMain -ne $ReproSpecCommit) {
    throw "origin/main does not match reproduction spec commit"
}

git bundle create $BundlePath $SourceRef
if ($LASTEXITCODE -ne 0) { throw "git bundle create failed" }

git bundle verify $BundlePath
if ($LASTEXITCODE -ne 0) { throw "git bundle verify failed" }

git bundle list-heads $BundlePath
if ($LASTEXITCODE -ne 0) { throw "git bundle list-heads failed" }

Get-FileHash -Algorithm SHA256 $BundlePath
```

### Offline Verification and Checkout

On the offline machine, re-verify the bundle SHA, then use explicit
`git init` + `git bundle verify` (inside repo) + `git fetch` with a
refspec. Bundle contains complete history. A plain `git clone` does not
auto-import remote-tracking refs into local branches.

Note: `git bundle verify` requires a Git repository. SHA-256 file check
does not. `BUNDLE_PATH` must be an absolute path because `git -C`
changes the working directory.

```bash
# Set variables
REPRO_SPEC_COMMIT="REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
BUNDLE_SOURCE_REF="refs/remotes/origin/main"
LOCAL_IMPORT_REF="refs/heads/reproduction-spec"
BUNDLE_PATH="$(pwd)/mergepilot-${REPRO_SPEC_COMMIT}.bundle"
CHECKOUT_DIR="mergepilot-clean-reproduction"

# File integrity check (does not need a Git repo)
sha256sum "$BUNDLE_PATH"

# Initialize empty repo (needed for git bundle verify)
git init "$CHECKOUT_DIR"

# Verify bundle integrity inside the repo
git -C "$CHECKOUT_DIR" bundle verify "$BUNDLE_PATH"

# Fetch from bundle using explicit refspec
git -C "$CHECKOUT_DIR" fetch "$BUNDLE_PATH" \
  "${BUNDLE_SOURCE_REF}:${LOCAL_IMPORT_REF}"

# Verify commit object exists
git -C "$CHECKOUT_DIR" cat-file -e "${REPRO_SPEC_COMMIT}^{commit}"
# Verify tree object exists
git -C "$CHECKOUT_DIR" cat-file -e "${REPRO_SPEC_COMMIT}^{tree}"

# Detached checkout
git -C "$CHECKOUT_DIR" checkout --detach "$REPRO_SPEC_COMMIT"

# Verify HEAD matches
test "$(git -C "$CHECKOUT_DIR" rev-parse HEAD)" = "$REPRO_SPEC_COMMIT"
# Verify imported ref matches
test "$(git -C "$CHECKOUT_DIR" rev-parse "$LOCAL_IMPORT_REF")" = "$REPRO_SPEC_COMMIT"
# Verify clean
test -z "$(git -C "$CHECKOUT_DIR" status --porcelain)"
```

Result: `source_acquisition_offline = true`, `source_archive_sha256 = "<recorded SHA-256>"`.

**Note**: `--all` works as a diagnostic fallback but includes all refs/tags
— not recommended for the formal minimal reproduction bundle.

## Competition Demo Paths

### Path 1: Local HTTP Server (Recommended)

```bash
# Windows:
cd samples\demo-console && python -m http.server 8080 --bind 127.0.0.1
# POSIX:
cd samples/demo-console && python3 -m http.server 8080 --bind 127.0.0.1
```

### Path 2: Direct File Open (Backup)

```bash
start samples\demo-console\index.html    # Windows
open samples/demo-console/index.html     # macOS
xdg-open samples/demo-console/index.html # Linux
```

### Path 3: Screenshots/Video (Failure Backup)
**Must state**: "Recording of REPLAY Console, not a live run."

## Server Lifecycle

| Step | Action | Verification |
|------|--------|-------------|
| Start | `python -m http.server --bind 127.0.0.1` | Record PID |
| Serve | Browser → `http://127.0.0.1:8080` | All 8 pages render |
| Stop | Ctrl+C | Process exits |
| Verify PID | Check process list | PID not found |
| Verify port | Check listening ports | Port not listening |

`server_process_residue = 0` and `listening_port_residue = 0` only if
both verifications pass.

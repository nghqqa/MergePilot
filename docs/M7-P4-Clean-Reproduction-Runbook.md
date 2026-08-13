# M7-P4 Clean Reproduction Runbook (v3 corrected)

**Status**: Design only — NOT yet executed
**Artifact baseline commit**: `148762091447754a50790441144968a12360844f`

## 1. Prerequisites

### System tools required

| Tool | Required | Reason |
|------|----------|--------|
| Python interpreter | Yes | Run schema/builder/render/serve/tests |
| Git CLI | Yes | Source checkout; `bundle_builder.py` calls `git rev-parse HEAD`; tests verify git blob SHAs |
| Web browser | Optional | Page display (not needed for verification) |

**Planned primary Python version**: 3.9 (`PLANNED_NOT_YET_CLEAN_VERIFIED`).
**Minimum supported version**: NOT_YET_VERIFIED.
**Candidate minimum**: 3.8 (hypothetical, not tested).

### Python packages

- `python_package_dependencies = []` (empty)
- `pip_install_required = false`
- Zero third-party packages. Stdlib only.

### NOT required

Docker, PostgreSQL, LLM API, SLS, GitHub (at runtime).

## 2. Clean Checkout

### 2.1 Create isolation directory

```bash
mkdir -p ~/m7-clean-repro
cd ~/m7-clean-repro
```

### 2.2 Acquire source

#### Option A: GitHub clone (requires network for clone only)

```bash
# Set the reproduction spec commit (frozen after M7-P4 design PR merges)
REPRO_SPEC_COMMIT="REPLACE_WITH_FULL_MERGE_COMMIT_SHA"

git clone https://github.com/nghqqa/MergePilot.git .
git checkout "$REPRO_SPEC_COMMIT"
# source_acquisition_offline = false
# Subsequent steps are fully offline
```

#### Option B: Pre-prepared git bundle (fully offline)

The bundle is created from the named ref `origin/main` (not a bare SHA).
A bare SHA produces `fatal: Refusing to create empty bundle`.
Before creation, verify that `origin/main` equals `reproduction_spec_commit`.

##### B.1 Prepare bundle on networked machine (POSIX)

```bash
# Set the reproduction spec commit (frozen after M7-P4 design PR merges)
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

##### B.2 Prepare bundle on networked machine (Windows PowerShell)

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

##### B.3 Offline fetch and checkout (POSIX)

Bundle contains complete history. A plain `git clone` does not auto-import
remote-tracking refs into local branches. The offline flow must use explicit
`git init` + `git fetch` with a refspec.

Note: `git bundle verify` requires a Git repository. SHA-256 file check
does not. Therefore the order is: SHA-256 first, then `git init`, then
`git bundle verify` inside the repo. `BUNDLE_PATH` must be an absolute
path because `git -C` changes the working directory.

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
# source_acquisition_offline = true
```

##### B.4 Offline fetch and checkout (Windows PowerShell)

```powershell
$ReproSpecCommit = "REPLACE_WITH_FULL_MERGE_COMMIT_SHA"
$BundleSourceRef = "refs/remotes/origin/main"
$LocalImportRef = "refs/heads/reproduction-spec"
$BundlePath = (Resolve-Path "mergepilot-$ReproSpecCommit.bundle").Path
$CheckoutDir = "mergepilot-clean-reproduction"

Get-FileHash -Algorithm SHA256 $BundlePath

git init $CheckoutDir
if ($LASTEXITCODE -ne 0) { throw "git init failed" }

git -C $CheckoutDir bundle verify $BundlePath
if ($LASTEXITCODE -ne 0) { throw "bundle verification failed" }

$FetchSpec = "${BundleSourceRef}:${LocalImportRef}"
git -C $CheckoutDir fetch $BundlePath $FetchSpec
if ($LASTEXITCODE -ne 0) { throw "bundle fetch failed" }

git -C $CheckoutDir cat-file -e "$ReproSpecCommit^{commit}"
if ($LASTEXITCODE -ne 0) { throw "commit object unavailable" }

git -C $CheckoutDir cat-file -e "$ReproSpecCommit^{tree}"
if ($LASTEXITCODE -ne 0) { throw "tree object unavailable" }

git -C $CheckoutDir checkout --detach $ReproSpecCommit
if ($LASTEXITCODE -ne 0) { throw "checkout failed" }

$ActualHead = (git -C $CheckoutDir rev-parse HEAD).Trim()
if ($ActualHead -ne $ReproSpecCommit) { throw "checkout commit mismatch" }

$ImportedCommit = (git -C $CheckoutDir rev-parse $LocalImportRef).Trim()
if ($ImportedCommit -ne $ReproSpecCommit) { throw "imported ref mismatch" }

$Dirty = git -C $CheckoutDir status --porcelain
if ($Dirty) { throw "checkout is dirty" }
# source_acquisition_offline = true
```

### 2.3 Verify clean worktree

```bash
git status --porcelain
# Must output nothing
git rev-parse HEAD
# Must output the reproduction spec commit SHA
```

## 3. Layer A: Artifact Replay Verification

### 3.1 Verify Bundle schema and SHA

```bash
python -I -B -c "
import json, sys
sys.path.insert(0, 'tools/demo_console')
from schema import validate_bundle
from bundle_builder import compute_bundle_sha256
bundle = json.load(open('samples/demo-bundles/m7-rag-replay.json'))
errors = validate_bundle(bundle)
print('schema_errors:', len(errors))
recomputed = compute_bundle_sha256(bundle)
print('sha_match:', bundle['bundle_sha256'] == recomputed)
print('demo_mode:', bundle['demo_mode'])
print('bundle_source_commit:', bundle['source_commit'])
print('bundle_verification_commit:', bundle['verification_commit'])
"
```

### 3.2 Verify evidence SHAs (file content bytes from git)

```bash
python -I -B -c "
import json, hashlib, subprocess
bundle = json.load(open('samples/demo-bundles/m7-rag-replay.json'))
commit = subprocess.check_output(['git','rev-parse','HEAD']).decode().strip()
for ef in bundle['evidence_files']:
    blob = subprocess.check_output(['git','show',commit+':'+ef['path']])
    actual = hashlib.sha256(blob).hexdigest()
    status = 'OK' if ef['sha256'] == actual else 'FAIL'
    print(f'{status} {ef[\"path\"]}: {actual[:24]}...')
"
```

**Note**: SHA-256 is of file **content bytes**, not Git blob object IDs.

### 3.3 Verify HTML

```bash
python -I -B -c "
import re
html = open('samples/demo-console/index.html').read()
pages = re.findall(r'<section[^>]*id=\"([^\"]+)\"', html)
print('page_count:', len(pages))
ext_src = re.findall(r'src=[\"\\']https?://', html, re.IGNORECASE)
ext_link = re.findall(r'<link', html)
print('external_reference_count:', len(ext_src) + len(ext_link))
print('mode_banner:', 'MODE: REPLAY' in html)
print('adopted_false:', 'adopted=False' in html)
print('untrusted_true:', 'untrusted=True' in html)
"
```

**Note**: `external_reference_count` is from HTML static scan, NOT network proof.

## 4. Layer B: Test Reproduction (unittest)

### 4.1 Run tests

```bash
# Windows (PowerShell):
python -I -B -m unittest discover -s tests/demo_console -p "test_*.py" -v

# POSIX:
python3 -I -B -m unittest discover -s tests/demo_console -p "test_*.py" -v
```

### 4.2 Record results

```
tests_run: <actual>
test_failures: <actual>
test_errors: <actual>
test_skipped: <actual>
```

Expected (in `expected_gates`, NOT in actual fields):
```
expected_tests_run: 33
expected_test_failures: 0
expected_test_errors: 0
```

### 4.3 Post-test verification

```bash
git status --porcelain   # empty
git diff --check          # exit 0
find . -type d -name __pycache__ -not -path './.git/*' 2>/dev/null | wc -l  # 0
find . -name .pytest_cache -not -path './.git/*' 2>/dev/null | wc -l       # 0
```

## 5. Network Observation (optional)

### Disabled-network test
1. Disable network adapter.
2. Open `samples/demo-console/index.html`.
3. Verify all 8 pages render.
4. Record `replay_succeeded_with_network_disabled`.

### Browser network log
1. DevTools → Network tab.
2. Navigate all 8 pages.
3. Record `observed_external_network_requests`.

If no observer: `browser_network_observation_status = "NOT_MEASURED"`,
`observed_external_network_requests = null`.

## 6. Competition Demo Launch

### Primary: local HTTP server

```powershell
# Windows:
cd samples\demo-console
python -m http.server 8080 --bind 127.0.0.1
```
```bash
# POSIX:
cd samples/demo-console
python3 -m http.server 8080 --bind 127.0.0.1
```

### Backup: direct file open

```bash
start samples\demo-console\index.html   # Windows
open samples/demo-console/index.html    # macOS
xdg-open samples/demo-console/index.html # Linux
```

### Failure backup: screenshots/video
**Must state**: "Recording of REPLAY Console, not a live run."

### Server lifecycle
1. Record PID at start.
2. Ctrl+C to stop.
3. Verify PID gone, port closed.
4. Record `server_process_residue = 0`, `listening_port_residue = 0`.

## 7. Reproduction Checklist

- [ ] Clean directory outside dev worktree
- [ ] Source acquired (clone or verified bundle)
- [ ] HEAD matches expected commit
- [ ] `git status --porcelain` empty
- [ ] Bundle schema valid
- [ ] Bundle SHA recomputable
- [ ] 5 evidence content SHAs match
- [ ] 8 HTML pages present
- [ ] `external_reference_count` scanned
- [ ] REPLAY/adopted/untrusted boundaries displayed
- [ ] unittest with `-I -B`
- [ ] `tests_run` recorded
- [ ] `git diff --check = 0`
- [ ] Worktree clean
- [ ] No pycache / pytest_cache / temp residue
- [ ] Server PID terminated, port closed

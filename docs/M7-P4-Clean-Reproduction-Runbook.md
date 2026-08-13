# M7-P4 Clean Reproduction Runbook (v2 corrected)

**Status**: Design only — NOT yet executed
**Base commit**: `148762091447754a50790441144968a12360844f`

## 1. Prerequisites

- Git CLI
- Python 3.8+ (stdlib only — no pip packages needed)
- A web browser (for HTML viewing)
- **No** Docker, PostgreSQL, LLM API, SLS, or GitHub access required at runtime
- **No** pip install required (stdlib only)

## 2. Clean Checkout

### 2.1 Create isolation directory

```bash
# Create a NEW directory OUTSIDE the development worktree
mkdir -p ~/m7-clean-repro
cd ~/m7-clean-repro
```

### 2.2 Acquire source

**Option A: GitHub clone (requires network for clone only)**
```bash
git clone https://github.com/nghqqa/MergePilot.git .
git checkout 148762091447754a50790441144968a12360844f
# source_acquisition_offline = false (network used for clone)
# Subsequent steps are fully offline.
```

**Option B: Pre-prepared git bundle (fully offline)**
```bash
# On a networked machine:
git bundle create mergepilot-1487620.bundle --all
sha256sum mergepilot-1487620.bundle
# Record source_archive_sha256

# On the offline machine:
git clone mergepilot-1487620.bundle .
git checkout 148762091447754a50790441144968a12360844f
# source_acquisition_offline = true
```

### 2.3 Verify clean worktree

```bash
git status --porcelain
# Must output nothing (empty = clean)
git rev-parse HEAD
# Must output: 148762091447754a50790441144968a12360844f
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
print('source_commit:', bundle['source_commit'])
print('verification_commit:', bundle['verification_commit'])
"
```

### 3.2 Verify evidence SHAs (file content bytes from git)

```bash
python -I -B -c "
import json, hashlib, subprocess
bundle = json.load(open('samples/demo-bundles/m7-rag-replay.json'))
commit = subprocess.check_output(['git','rev-parse','HEAD']).decode().strip()
for ef in bundle['evidence_files']:
    # Read authoritative bytes from git blob
    blob = subprocess.check_output(['git','show',commit+':'+ef['path']])
    actual = hashlib.sha256(blob).hexdigest()
    status = 'OK' if ef['sha256'] == actual else 'FAIL'
    print(f'{status} {ef[\"path\"]}: {actual[:24]}...')
"
```

**Note**: This computes SHA-256 of file **content bytes**, not Git blob
object IDs. Git blob IDs include header bytes; content SHA does not.

### 3.3 Verify HTML (pages, external refs)

```bash
python -I -B -c "
import re
html = open('samples/demo-console/index.html').read()
pages = re.findall(r'<section[^>]*id=\"([^\"]+)\"', html)
print('page_count:', len(pages))
print('pages:', pages)
ext_src = re.findall(r'src=[\"\\']https?://', html, re.IGNORECASE)
ext_link = re.findall(r'<link', html)
print('external_reference_count:', len(ext_src) + len(ext_link))
print('mode_banner:', 'MODE: REPLAY' in html)
print('adopted_false:', 'adopted=False' in html)
print('untrusted_true:', 'untrusted=True' in html)
"
```

**Note**: `external_reference_count = 0` is from HTML static scan.
It does NOT prove zero network requests. For that, use a network observer
or disabled-network test (see §5).

## 4. Layer B: Test Reproduction (unittest, not pytest)

### 4.1 Run tests

```bash
# Windows (PowerShell) — single line:
python -I -B -m unittest discover -s tests/demo_console -p "test_*.py" -v

# POSIX (bash):
python3 -I -B -m unittest discover -s tests/demo_console -p "test_*.py" -v
```

- `-I`: isolated mode (no user site-packages, no PYTHONPATH).
- `-B`: no `__pycache__` generation.
- No pytest, no `.pytest_cache`.

### 4.2 Record results

```
tests_run: <actual count from unittest output>
test_failures: <actual>
test_errors: <actual>
test_skipped: <actual>
```

Expected (in `expected_gates`, NOT in actual result fields):
```
expected_tests_run: 33
expected_test_failures: 0
expected_test_errors: 0
```

### 4.3 Post-test verification

```bash
git status --porcelain
# Must be empty (no worktree changes)

git diff --check
# Must exit 0

# Verify no pycache (should be none with -B)
find . -type d -name __pycache__ -not -path './.git/*' 2>/dev/null | wc -l
# Must be 0

# Verify no pytest_cache (should be none — using unittest)
find . -name .pytest_cache -not -path './.git/*' 2>/dev/null | wc -l
# Must be 0
```

## 5. Network Observation (optional but recommended)

### 5.1 Disabled-network test

1. Disable network adapter (or use isolated network).
2. Open `samples/demo-console/index.html` in browser.
3. Verify all 8 pages render correctly.
4. Record: `replay_succeeded_with_network_disabled = true`.

### 5.2 Browser network log

1. Open browser DevTools → Network tab.
2. Navigate through all 8 pages.
3. Check for any external requests.
4. Record: `observed_external_network_requests = <count>`.

If no network observer is used:
```
browser_network_observation_status = "NOT_MEASURED"
observed_external_network_requests = null
```

## 6. Competition Demo Launch

### 6.1 Primary path: local HTTP server

**Windows (PowerShell):**
```powershell
cd samples\demo-console
python -m http.server 8080 --bind 127.0.0.1
# Open: http://127.0.0.1:8080
# Ctrl+C to stop
```

**POSIX (bash):**
```bash
cd samples/demo-console
python3 -m http.server 8080 --bind 127.0.0.1
# Open: http://127.0.0.1:8080
# Ctrl+C to stop
```

Or using serve.py (read-only, blocks PUT/POST/DELETE/PATCH):
```bash
python tools/demo_console/serve.py --port 8080
```

### 6.2 Backup path: direct file open

```bash
# Windows
start samples\demo-console\index.html
# macOS
open samples/demo-console/index.html
# Linux
xdg-open samples/demo-console/index.html
```

### 6.3 Failure backup: screenshots/video

Pre-captured screenshots or recorded video.
**Must state**: "This is a recording of the REPLAY Console, not a live run."

### 6.4 Server lifecycle verification

After demo:
1. Press Ctrl+C to stop server.
2. Verify server PID no longer exists.
3. Verify port 8080 not listening.
4. Record `server_process_residue = 0`, `listening_port_residue = 0`.

## 7. Reproduction Checklist

- [ ] Clean directory created outside dev worktree
- [ ] Source acquired (clone or bundle)
- [ ] `git rev-parse HEAD` matches `1487620...`
- [ ] `git status --porcelain` empty
- [ ] Bundle schema valid (0 errors)
- [ ] Bundle SHA recomputable
- [ ] 5 evidence content SHAs match (from git blob bytes)
- [ ] 8 HTML pages present
- [ ] `external_reference_count = 0` (HTML scan)
- [ ] REPLAY mode banner displayed
- [ ] adopted=false, untrusted=true displayed
- [ ] runtime_consumes_rag_context=false displayed
- [ ] unittest run with `-I -B` flags
- [ ] tests_run recorded (expected 33)
- [ ] git diff --check = 0
- [ ] Worktree clean after tests
- [ ] No pycache residue (verify with find)
- [ ] No pytest_cache residue
- [ ] No temp file residue
- [ ] Server PID terminated after demo
- [ ] Port not listening after demo

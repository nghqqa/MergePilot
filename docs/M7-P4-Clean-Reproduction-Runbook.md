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

**Option A: GitHub clone (requires network for clone only)**

```bash
# Set the reproduction spec commit (frozen after M7-P4 design PR merges):
REPRO_SPEC_COMMIT="<PR merge commit full SHA>"

git clone https://github.com/nghqqa/MergePilot.git .
git checkout "$REPRO_SPEC_COMMIT"
# source_acquisition_offline = false (network used for clone)
# Subsequent steps are fully offline.
```

**Option B: Pre-prepared git bundle (fully offline)**

Prepare on networked machine (POSIX):
```bash
REPRO_SPEC_COMMIT="<PR merge commit full SHA>"
BUNDLE_PATH="mergepilot-${REPRO_SPEC_COMMIT}.bundle"

git bundle create "$BUNDLE_PATH" "$REPRO_SPEC_COMMIT"
git bundle verify "$BUNDLE_PATH"
sha256sum "$BUNDLE_PATH"
```

Prepare on networked machine (Windows PowerShell):
```powershell
$ReproSpecCommit = "<PR merge commit full SHA>"
$BundlePath = "mergepilot-$ReproSpecCommit.bundle"

git bundle create $BundlePath $ReproSpecCommit
git bundle verify $BundlePath
Get-FileHash -Algorithm SHA256 $BundlePath
```

Transfer bundle to offline machine, then (POSIX):
```bash
REPRO_SPEC_COMMIT="<PR merge commit full SHA>"
BUNDLE_PATH="mergepilot-${REPRO_SPEC_COMMIT}.bundle"

# Re-verify SHA-256 matches recorded value:
sha256sum "$BUNDLE_PATH"

# Clone from bundle:
git clone "$BUNDLE_PATH" .
git checkout "$REPRO_SPEC_COMMIT"
# source_acquisition_offline = true
# source_archive_sha256 = <recorded SHA-256>
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

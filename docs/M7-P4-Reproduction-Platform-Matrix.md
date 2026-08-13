# M7-P4 Reproduction Platform Matrix (v2 corrected)

**Status**: Design only

## Environment Matrix

| Dimension | Primary | Backup | Failure Backup |
|-----------|---------|--------|----------------|
| **OS** | Windows 10/11 (PowerShell) | MergePilot-Test WSL (bash) | Any OS with Python 3.8+ |
| **Python** | 3.9.x (verified) | 3.10.x (WSL, verified) | 3.8+ (NOT YET VERIFIED) |
| **Browser** | Chrome/Edge | Firefox | Direct file open |
| **HTTP server** | `python -m http.server --bind 127.0.0.1` | `serve.py` | Direct file open |
| **Network (runtime)** | Not required | Not required | Not required |
| **Network (clone)** | Required (HTTPS) | Not required (bundle) | N/A |
| **Docker** | Not needed | Not needed | Not needed |
| **PostgreSQL** | Not needed | Not needed | Not needed |
| **LLM API** | Not needed | Not needed | Not needed |
| **SLS** | Not needed | Not needed | Not needed |
| **GitHub** | Not needed (runtime) | Not needed | Not needed |

## Python Version Status

| Version | Status | Evidence |
|---------|--------|----------|
| 3.9.25 (Windows) | **VERIFIED** | 33 unittest cases passed |
| 3.10.12 (WSL) | **VERIFIED** | M4-E/M7 tests run on this version |
| 3.8.x | **NOT_YET_VERIFIED** | Hypothetical only — f-strings supported but untested |

`candidate_minimum_python_version = "3.8"`
`minimum_python_version_status = "NOT_YET_VERIFIED"`

Only after running the full unittest suite on Python 3.8 may the
`supported_python_versions` list include `"3.8"`.

## Dependency Analysis

Demo Console modules and their imports:

| Module | Imports | Third-party? |
|--------|---------|-------------|
| `schema.py` | json, re | No (stdlib) |
| `bundle_builder.py` | json, hashlib, os, re, sys, subprocess, time, pathlib | No (stdlib) |
| `render.py` | json, os, sys, pathlib, re | No (stdlib) |
| `serve.py` | argparse, http.server, os, socketserver, sys, pathlib | No (stdlib) |
| `test_demo_console.py` | json, hashlib, os, re, sys, unittest, pathlib, tempfile, subprocess | No (stdlib) |

**Conclusion**: `dependency_bootstrap_offline = true`,
`dependency_bootstrap_requires_network = false`.
No pip, no venv, no lock file needed.

## Source Acquisition Modes

### Mode A: GitHub Clone (network for clone only)

```
source_acquisition_mode = "github_https_clone"
source_acquisition_offline = false
# Network used ONLY for git clone.
# All subsequent steps (replay, tests) are offline.
```

### Mode B: Pre-prepared Git Bundle (fully offline)

```
source_acquisition_mode = "git_bundle"
source_acquisition_offline = true
source_archive_sha256 = "<SHA-256 of .bundle file>"
# Bundle prepared from pinned origin/main commit.
# No network at any point.
```

## Competition Live Demo Paths

### Path 1: Local HTTP Server (Recommended)

```bash
# Windows:
cd samples\demo-console
python -m http.server 8080 --bind 127.0.0.1

# POSIX:
cd samples/demo-console
python3 -m http.server 8080 --bind 127.0.0.1
```
Open browser to `http://127.0.0.1:8080`.
Ctrl+C to stop.

- Pros: Clean URL, tab navigation works.
- Cons: Requires terminal running.
- Binds to localhost only — no LAN exposure.

### Path 2: Direct File Open (Backup)

```bash
# Windows: start samples\demo-console\index.html
# macOS: open samples/demo-console/index.html
# Linux: xdg-open samples/demo-console/index.html
```

- Pros: Zero terminal needed.
- Cons: `file://` protocol may affect some JS.

### Path 3: Screenshots/Video (Failure Backup)

- Pre-captured screenshots of all 8 pages.
- 5-minute screen recording.
- **Must state**: "This is a recording of the REPLAY Console, not a live run."

## Server Lifecycle

| Step | Action | Verification |
|------|--------|-------------|
| Start | `python -m http.server --bind 127.0.0.1` | Record PID |
| Serve | Browser accesses `http://127.0.0.1:8080` | All 8 pages render |
| Stop | Ctrl+C | Process exits |
| Verify PID | `taskkill /FI "PID eq <pid>"` (Windows) or `kill -0 <pid>` (POSIX) | PID not found |
| Verify port | `netstat -an | findstr 8080` (Windows) or `ss -tlnp | grep 8080` (POSIX) | Port not listening |

`server_process_residue = 0` and `listening_port_residue = 0` only if
both verifications pass.

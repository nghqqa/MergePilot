#!/usr/bin/env python3
"""Enable/disable AgentLoop OTel in a running worker container (no restart).

Writes /etc/agentloop-otel.json via `docker exec -i` (JSON over stdin: the license
key never appears in argv / `ps`). The zz_agentloop_otel watcher inside the container
picks the change up within ~2s and patches after its grace period.

Usage:
  AGENTLOOP_LICENSE_KEY=... python enable_otel.py on  <container> [<container>...]
  python enable_otel.py off <container> [...]
  python enable_otel.py status <container> [...]
"""
import json
import os
import subprocess
import sys

ENDPOINT = ("https://proj-xtrace-98a0a58e103d37e424adc3fdffb5f51-cn-hangzhou."
            "cn-hangzhou.log.aliyuncs.com/apm/trace/opentelemetry/v1/traces")
PROJECT = "proj-xtrace-98a0a58e103d37e424adc3fdffb5f51-cn-hangzhou"
WORKSPACE = "agentloop-ef01c1066efd4ea4ef06a75d91e8fde0"


def cfg_on(key):
    return {
        "enabled": True,
        "otlp": ENDPOINT,
        "headers": {"x-arms-license-key": key, "x-arms-project": PROJECT, "x-cms-workspace": WORKSPACE},
        "workspace": WORKSPACE,
        "service_name": "mergepilot-copaw",
        "schedule_delay_ms": 3000,
    }


def cfg_off():
    return {"enabled": False, "otlp": "", "headers": {}, "workspace": ""}


def write(container, cfg):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    p = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", "cat > /etc/agentloop-otel.json"],
                       input=json.dumps(cfg).encode(), env=env, capture_output=True)
    return p.returncode == 0, p.stderr.decode()[:200]


def status(container):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    p = subprocess.run(["docker", "exec", container, "sh", "-c",
                        "python3 -c \"import json;c=json.load(open('/etc/agentloop-otel.json'));print('enabled=',c.get('enabled'),'otlp=',c.get('otlp','')[:70],'has_key=',bool((c.get('headers') or {}).get('x-arms-license-key')))\" 2>/dev/null || cat /etc/agentloop-otel.json | head -c 200; echo; echo '--- audit ---'; tail -n 15 /tmp/agentloop-model-audit.log 2>/dev/null || echo '(no audit log yet)'; echo '--- span counts ---'; sort /tmp/agentloop-spans.log 2>/dev/null | uniq -c | sort -rn || echo '(no spans yet)'"],
                       env=env, capture_output=True, text=True)
    return p.stdout + p.stderr


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    mode, containers = sys.argv[1], sys.argv[2:]
    if mode == "on":
        key = os.environ.get("AGENTLOOP_LICENSE_KEY", "").strip()
        if not key:
            print("AGENTLOOP_LICENSE_KEY env var is required for 'on'")
            sys.exit(2)
        for c in containers:
            ok, err = write(c, cfg_on(key))
            print(f"{c}: {'ENABLED' if ok else 'FAILED ' + err}")
    elif mode == "off":
        for c in containers:
            ok, err = write(c, cfg_off())
            print(f"{c}: {'DISABLED' if ok else 'FAILED ' + err}")
    elif mode == "status":
        for c in containers:
            print(f"===== {c} =====")
            print(status(c))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()

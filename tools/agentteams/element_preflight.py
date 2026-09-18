#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""element_preflight.py — READ-ONLY readiness check for a real AgentTeams/Element rework run.

Never starts, stops, creates or deletes anything. It only inspects the existing p14h2 stack
(container states, networks, host ports, presence of configuration variable NAMES — values are
never read or printed) and the local rework case, then prints what is ready and which items
still need an explicit authorization. Exit 0 = report produced; exit 2 = docker unreachable.

Usage: python tools/agentteams/element_preflight.py [--json]
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CASE = REPO_ROOT / "tools" / "agentteams" / "rework_case"

REQUIRED_CONTAINERS = {
    "p14h2-wd-ctrl": "AgentTeams embedded controller (Matrix homeserver :6167, MinIO :9000, API :8090; spawns workers via docker.sock)",
    "agentteams-manager": "AgentTeams leader/manager (holds LLM provider/model + AI gateway config)",
    "agentteams-worker-p14h2-copaw-worker-reviewer": "CoPaw Reviewer",
    "agentteams-worker-p14h2-copaw-worker-fixer": "CoPaw Fixer",
    "agentteams-worker-p14h2-copaw-worker-verifier": "CoPaw Verifier",
    "agentteams-worker-p14h2-copaw-worker-manager": "CoPaw manager worker",
    "p14h2-wd-element-web": "Element Web (127.0.0.1:18088) — the human-facing handoff view",
}
OPTIONAL_CONTAINERS = ["agentteams-worker-p14h2-wd-worker-reviewer", "agentteams-worker-p14h2-wd-worker-fixer",
                       "agentteams-worker-p14h2-wd-worker-verifier", "agentteams-worker-p14h2-wd-worker-manager"]
REQUIRED_NETWORKS = ["p14h2-wd-net", "p14h2-copaw-net"]
HOST_PORTS = {18088: "element-web", 6167: "matrix homeserver (ctrl)", 8090: "controller API (ctrl)", 9000: "minio (ctrl)"}
# variable NAMES whose presence we confirm; values are never read
ENV_NAMES = {
    "p14h2-wd-ctrl": ["AGENTTEAMS_MATRIX_URL", "AGENTTEAMS_MATRIX_DOMAIN", "AGENTTEAMS_DEFAULT_MODEL", "AGENTTEAMS_LLM_API_KEY",
                      "AGENTTEAMS_GITHUB_TOKEN", "AGENTTEAMS_COPAW_WORKER_IMAGE", "AGENTTEAMS_DOCKER_NETWORK"],
    "agentteams-manager": ["AGENTTEAMS_LLM_PROVIDER", "AGENTTEAMS_DEFAULT_MODEL", "AGENTTEAMS_AI_GATEWAY_URL", "AGENTTEAMS_MANAGER_MATRIX_TOKEN"],
    "agentteams-worker-p14h2-copaw-worker-reviewer": ["AGENTTEAMS_WORKER_ROOM_ID", "AGENTTEAMS_WORKER_ROLE", "AGENTTEAMS_MATRIX_URL"],
}


def docker(args):
    try:
        out = subprocess.run(["docker"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace")


def container_state(name):
    raw = docker(["inspect", name, "--format", "{{.State.Status}}|{{.Config.Image}}|{{json .NetworkSettings.Networks}}"])
    if raw is None:
        return {"exists": False}
    status, image, nets = raw.strip().split("|", 2)
    return {"exists": True, "status": status, "image": image, "networks": sorted(json.loads(nets).keys())}


def env_names_present(name, wanted):
    raw = docker(["inspect", name, "--format", "{{range .Config.Env}}{{println .}}{{end}}"])
    if raw is None:
        return {w: None for w in wanted}
    names = {line.split("=", 1)[0] for line in raw.splitlines() if line}
    return {w: (w in names) for w in wanted}


def port_free(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if docker(["info", "--format", "{{.ServerVersion}}"]) is None:
        print("docker engine unreachable — cannot inspect the stack (nothing was started)", file=sys.stderr)
        return 2
    report = {"mode": "READ_ONLY_PREFLIGHT", "started_or_changed_anything": False,
              "containers": {}, "optional_containers": {}, "networks": {}, "host_ports": {}, "env_names": {}, "rework_case": {}, "authorization_required": []}
    for name, role in REQUIRED_CONTAINERS.items():
        st = container_state(name)
        st["role"] = role
        report["containers"][name] = st
    for name in OPTIONAL_CONTAINERS:
        report["optional_containers"][name] = container_state(name)
    nets = docker(["network", "ls", "--format", "{{.Name}}"]) or ""
    for n in REQUIRED_NETWORKS:
        report["networks"][n] = n in nets.split()
    for port, who in HOST_PORTS.items():
        report["host_ports"][port] = {"expected_owner": who, "free_now": port_free(port)}
    for name, wanted in ENV_NAMES.items():
        report["env_names"][name] = env_names_present(name, wanted)
    for rel in ("base/payments.py", "base/test_payments.py", "attempt1/payments.py", "attempt2/payments.py", "reviewer_findings.json", "element_messages.md"):
        report["rework_case"][rel] = (CASE / rel).is_file()
    report["authorization_required"] = [
        "A1 start the existing team containers listed above (no stop/delete/modify of any other container, image, volume or network)",
        "A2 use the Matrix room referenced by AGENTTEAMS_WORKER_ROOM_ID (value to be confirmed by the operator; not read here)",
        "A3 paid LLM calls through the configured provider/model (AGENTTEAMS_LLM_PROVIDER / AGENTTEAMS_DEFAULT_MODEL) within the agreed cost cap",
        "A4 GitHub writes on the test repository nghqqa/fastapi-boilerplate-demo: create one branch + one PR for the rework case; fix commits on that branch only; no merge, no main write",
        "A5 (optional, second tier) start the MergePilot isolated stack and join its Workflow Controller to the same room for PG-authoritative state",
    ]
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for name, st in report["containers"].items():
            print("%-48s %s" % (name, st.get("status", "MISSING") if st["exists"] else "MISSING"))
        print("networks:", report["networks"])
        print("host ports:", {p: v["free_now"] for p, v in report["host_ports"].items()})
        print("env names present:", report["env_names"])
        print("rework case files:", report["rework_case"])
        print("\nauthorization required before any run:")
        for a in report["authorization_required"]:
            print("  -", a)
    return 0


if __name__ == "__main__":
    sys.exit(main())

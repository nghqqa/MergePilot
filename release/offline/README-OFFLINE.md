# MergePilot offline stack - quick start (Windows / WSL2 / Linux)

Fixes the schema gap seen in the 0912 external-machine test
(`relation "task_runs" does not exist`): this bundle ships the DB
migrations and mounts them into PostgreSQL's init directory.

## Prerequisites
- Docker Desktop (WSL2 backend) running, images already loaded
  (`load-images.ps1` / `load-images.sh` from the images bundle).
- No source checkout needed; no network needed.

## Start (one command)
- Windows:  double-click `start-stack.bat`   (or: `.\start-stack.bat`)
- WSL2/Linux: `bash start-stack.sh`

The script: (1) starts postgres alone to measure the compose bridge IP,
(2) writes `.env` with that IP, (3) starts the full stack. On a fresh
volume, `db-init/*.sql` initializes the full audit schema, roles, and the
demo run row before the controller starts.

## Verify
    docker compose ps -a
    -> 6 services "Up (healthy)"; preflight exits 0 (one-shot gate, PREFLIGHT_OK)
    curl http://127.0.0.1:8600/api/live/status   -> 200
    curl http://127.0.0.1:8090/healthz           -> 200
    console UI: http://127.0.0.1:8600

## Files
- docker-compose.yml   stack definition (schema init mount + digest-pin env)
- db-init/             000 roles, 001 audit-db migrations (topo-sorted),
                       002 console migrations, 003 runtime logins,
                       004 environment marker, 005 demo run seed
- start-stack.bat/.sh  two-phase launcher (measures the bridge IP for the
                       console WRONG_SERVER pin; compose requires the var)
- env.templates.md     what each env file contains (smoke values preseeded)
- postgres.env / controller.env / gh_webhook.env / demo_console.env

## Notes
- `hiclaw-controller` (AgentTeams Matrix) is not part of this 7-service
  stack; the controller logs `Matrix degraded ... (L2 域继续运行)` and the
  L2 audit domain keeps running - expected in offline mode.
- Passwords are fixed smoke values; do not expose ports beyond loopback.

## Revision
- **rev2 (20260913)**: seed run_id in `db-init/005-seed.sql` aligned with the
  launcher's `MERGEPILOT_RUN_ID=offline-demo` (rev1 shipped `schema-test`,
  which tripped the console's fail-closed RUN_NOT_FOUND gate). Both files now
  carry the same literal; verify you have rev2 via SHA256SUMS in this folder.

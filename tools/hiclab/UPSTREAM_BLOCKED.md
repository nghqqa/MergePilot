# UPSTREAM_BLOCKED: HiClaw worker-creation hardening

## Status: BLOCKED_UPSTREAM (option b)

**No socket-proxy daemon is implemented.** The Manager auto-create path for
`hiclaw-worker-*` is NOT hardened. This round explicitly does NOT claim that
worker creation prevention is complete. ``harden_policy.py`` is a PURE
STRATEGY module with unit tests only -- it is NOT a deployed proxy and does
NOT constitute a closed real creation chain.

### Scope of THIS delivery (narrowed claim)

This delivery is accurately described ONLY as:

  **disk guard + guarded base-service startup + cleanup tooling candidate**

It is NOT "complete worker storage hardening". Worker-creation hardening
(Manager auto-create path) remains BLOCKED_UPSTREAM.

### Programmatic enforcement (not a documentation convention)

Because neither a deployed+audited socket proxy nor a confirmed upstream
disable-auto-create capability exists, ``guarded_start.py`` PROGRAMMATICALLY
refuses to start **both** ``hiclaw-controller`` **and** ``hiclaw-manager``
when no valid capability marker is present. ``manager_start_allowed()``
checks the marker files; when neither validates:

  * **0 production containers are started** (no partial stack).
  * ``hiclaw-controller`` is added to the ``blocked`` list alongside
    ``hiclaw-manager`` — the controller has docker-socket access and spawns
    workers / re-spawns containers via the HiClaw platform, constituting
    the same auto-create bypass as the manager.
  * If ``hiclaw-controller`` is already running, the guard **stops it**
    (it cannot be left running as a "previously-running" exception).
  * The supervisor emits ``BLOCKED_UPSTREAM`` and exits non-zero.

Rollback ordering: ``_rollback()`` stops ``hiclaw-controller`` **first**
(before other containers), then re-verifies remaining containers are stopped
(the controller may have re-spawned them before dying).

This is code, not a documentation rule. The full production stack is NOT
startable without a valid capability marker. D2B-3 remains non-runnable.

### Production operating restriction (option b)

Until a real, deployable, fail-closed Unix socket-proxy daemon exists and is
deployed, production operation is restricted:

  * **Manager auto-create of workers is FORBIDDEN.** The HiClaw Manager must
    not be allowed to spawn `hiclaw-worker-*` on its own. Operator policy +
    team config must prevent it.
  * The ONLY permitted worker creation path is the manual
    ``create_hardened_worker.sh`` operator tool (full contract, env-file
    secret safety, rollback artifact).
  * Any worker created by any other path is, by definition, UNHARDENED and
    must be treated as a policy violation.

Building a self-rolled security-critical Docker proxy in this round was
deliberately declined (option b over option a) to avoid shipping an
unaudited privilege boundary.

## The authoritative entry (confirmed)

```
docker exec hiclaw-controller hiclaw create worker --name <W> \
    --model <MODEL> --runtime openclaw
```

The CLI exposes ``--name``, ``--model``, ``--runtime`` but **no**
``--tmpfs`` / ``--storage-opt`` / container-spec injection. The HiClaw
Manager auto-creates workers through the same internal routine via the
docker socket bind-mounted into hiclaw-controller.

## Why a standalone wrapper does NOT prevent recurrence

``create_hardened_worker.sh`` is a **manual operator tool** (full contract,
env-file secret safety, rollback). It is NOT wired into the Manager's
auto-creation, so Manager-initiated creation BYPASSES it. Under option (b),
the operating restriction above forbids Manager auto-create to close this
gap procedurally until a proxy exists.

## harden_policy.py is a pure strategy module (not a proxy)

``harden_policy.py`` contains the request-matching + hardening-injection
logic that a future socket-proxy daemon would apply. Its unit tests
(``test_harden_policy.py``) verify the strategy in isolation. They are NOT
integration tests and do NOT prove any real creation chain is intercepted.
Do not describe them as such.

## What IS prevented this round (honestly)

  * **disk_guard** (deployed in startup scripts): fail-closed on host+guest
    free space. Prevents ENOSPC-driven amplification regardless of how
    containers are created.
  * **Guarded startup** (``hiclab_supervisor.sh`` -> ``guarded_start.py`` +
    transactional ``install_guarded.py`` + systemd unit): closes the boot
    auto-start bypass. Managed containers are restart=no; the supervisor is
    the only start path, gated by disk_guard + phased health checks.
  * ``start-controller-container.sh`` detects guarded mode and uses
    restart=no when active (reports UNPROTECTED otherwise).
  * ``minio_cleanup.py`` + ``cleanup_run.py``: **DRY-RUN-ONLY candidates**.
    Both ship no authoritative precondition probe, so ``--apply`` is
    fail-closed (stable status 3, no plan built, no mc/docker call, no
    traceback). ``cleanup_run.check_preconditions`` defines the required
    gate (RUN_ID in production records + run ended + evidence
    source_commit/binding verified) but no probe is wired in this candidate.

## NOT prevented (honest)

  * **hiclaw-manager START is code-enforced** (guarded_start refuses to
    start it without a valid capability marker -- see Programmatic
    enforcement above). This is a programmatic check, not a documentation
    convention.
  * **Worker auto-create is NOT independently code-prevented.** If the
    Manager were somehow running (e.g. started outside the supervisor), it
    could still create unhardened workers. There is no socket proxy to
    intercept ContainerCreate. The code-enforced Manager-start block is the
    primary guard; a proxy would be the secondary one. This is the gap a
    future proxy closes.
  * The 3x mirror amplification (MinIO -> controller -> worker) is not
    code-addressable without HiClaw sync-exclude support (upstream) or the
    proxy redirecting temp paths.

## Unblocking (future)

1. Implement a real fail-closed Unix socket-proxy daemon covering Docker API
   streaming, errors, timeouts, permissions, and request-body limits.
2. Deploy it as the socket hiclaw-controller talks to.
3. Then ``harden_policy.process_request`` becomes a live interceptor and
   Manager auto-create can be re-permitted.

Until then, option (b) stands: Manager auto-create forbidden, manual
``create_hardened_worker.sh`` only.

# Standalone Doctor Contract (m9-d)

## Offline mode (default, no flags)
Checks: runtime_layout (planner .py files), install_status (install.json),
pgvector pin, built images, stack classification, port availability.
Does NOT check: Dockerfiles, docker-compose.yml, migrations, audit-db SQL.
Result: DOCTOR_RUNTIME_OK + DOCTOR_LAYOUT_SOURCE_ONLY (advisory).

## Source-build mode (--build-from-source)
Checks: everything in offline mode PLUS Dockerfiles, docker-compose.yml,
migrations, audit-db SQL, room-map example.
Missing files → DOCTOR_LAYOUT_MISSING (fail).

## Decision matrix
| Mode | Dockerfiles missing | Runtime files missing | install.json missing |
|---|---|---|---|
| offline (default) | advisory (SOURCE_ONLY) | FAIL (RUNTIME_MISSING) | warning (NOT_INSTALLED) |
| --build-from-source | FAIL (LAYOUT_MISSING) | FAIL (RUNTIME_MISSING) | warning (NOT_INSTALLED) |

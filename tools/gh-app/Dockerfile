# gh-webhook service image (M8-GH-1 staging wiring; M8-GH-2 hardening).
#
# Contract:
#   - stdlib-only application code; the ONLY dependency is psycopg2-binary
#     (pinned) for the INSERT-only ingress DSN.
#   - secrets arrive via env-file (GITHUB_INGRESS_DSN / GITHUB_WEBHOOK_SECRET)
#     — never argv, never logs, never baked into the image.
#   - binds 0.0.0.0 INSIDE the container; host-side publication is
#     loopback-only and belongs to the orchestrator.
#   - healthz = process up + PG SELECT 1 (no table privileges required).
#   - M8-GH-2 §2: base pinned by digest resolved from the daemon this round
#     (python:3.12-slim @ sha256:9e869b0816f5537709825b49e62dc86d1c2691eff19b05c1d4dc3a07992cc052,
#      image ID 54a0b2beae90…, linux/amd64) — no bare tag.
#   - M8-GH-2 §3: dedicated non-root runtime user mergepilot-gh (UID/GID
#     9090/9090, no login shell); app files stay root-owned and read-only to
#     the runtime user; port 8090 is unprivileged; no sudo/capabilities/
#     host sockets; the service writes nothing under /app.

FROM python:3.12-slim@sha256:9e869b0816f5537709825b49e62dc86d1c2691eff19b05c1d4dc3a07992cc052

RUN pip install --no-cache-dir psycopg2-binary==2.9.12 \
 && groupadd -g 9090 mergepilot-gh \
 && useradd -u 9090 -g 9090 -M -s /usr/sbin/nologin mergepilot-gh

COPY tools/gh-app/__init__.py /app/gh_app/__init__.py
COPY tools/gh-app/receiver.py /app/gh_app/receiver.py
COPY tools/gh-app/http_server.py /app/gh_app/http_server.py
COPY tools/gh-app/checks_reporter.py /app/gh_app/checks_reporter.py
COPY tools/gh-app/token_provider.py /app/gh_app/token_provider.py
COPY tools/gh-app/dsn_guard.py /app/gh_app/dsn_guard.py
# M8-GH-4B2: hash-locked runtime deps for the reporter's RS256 JWT signing
# (cryptography + cffi + pycparser, linux/amd64 cp312/abi3 wheels; the
# receiver never imports them). No floating versions, no sdist fallback.
COPY tools/gh-app/requirements-reporter.lock /app/gh_app/requirements-reporter.lock

# Normalize modes: DrvFs build contexts can present 0777 source metadata;
# the contract (app files root-owned, runtime user non-writable) is
# enforced IN the image, independent of the build host filesystem.
RUN chmod 0644 /app/gh_app/*.py /app/gh_app/requirements-reporter.lock  && chmod 0755 /app /app/gh_app

RUN pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /app/gh_app/requirements-reporter.lock

WORKDIR /app/gh_app
ENV PYTHONUNBUFFERED=1 \
    GH_APP_BIND=0.0.0.0 \
    GH_APP_PORT=8090
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/healthz',timeout=2)"
USER mergepilot-gh
ENTRYPOINT ["python", "-u", "http_server.py"]

# gh-webhook service image (M8-GH-1 staging wiring).
#
# Contract:
#   - stdlib-only application code; the ONLY dependency is psycopg2-binary
#     (pinned) for the INSERT-only ingress DSN.
#   - secrets arrive via env-file (GITHUB_INGRESS_DSN / GITHUB_WEBHOOK_SECRET)
#     — never argv, never logs.
#   - binds 0.0.0.0 INSIDE the container; host-side publication is
#     loopback-only and belongs to the orchestrator (staging publishes
#     127.0.0.1::<port>).
#   - healthz = process up + PG SELECT 1 (no table privileges required).

FROM python:3.12-slim

RUN pip install --no-cache-dir psycopg2-binary==2.9.12

COPY tools/gh-app/__init__.py /app/gh_app/__init__.py
COPY tools/gh-app/receiver.py /app/gh_app/receiver.py
COPY tools/gh-app/http_server.py /app/gh_app/http_server.py
COPY tools/gh-app/checks_reporter.py /app/gh_app/checks_reporter.py

WORKDIR /app/gh_app
ENV PYTHONUNBUFFERED=1 \
    GH_APP_BIND=0.0.0.0 \
    GH_APP_PORT=8090
# Reporter loop is started separately by the orchestrator in staging
# (python -m checks_reporter.run style invocation is not needed: the loop
# entry is checks_reporter.run_loop via a tiny wrapper command).
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/healthz',timeout=2)"
ENTRYPOINT ["python", "-u", "http_server.py"]

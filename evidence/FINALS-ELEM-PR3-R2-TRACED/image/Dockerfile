# copaw-worker with AgentLoop OTel instrumentation baked in (v2 deferred design).
# Base: the P14-verified worker image. Adds only three files; no runtime behaviour
# changes while /etc/agentloop-otel.json stays enabled=false (the image default).
FROM agentteams/copaw-worker:223ddc2-build1

ARG SITE=/opt/venv/standard/lib/python3.11/site-packages
COPY zz_agentloop_otel.py ${SITE}/zz_agentloop_otel.py
COPY zzz_agentloop.pth    ${SITE}/zzz_agentloop.pth
COPY agentloop-otel.json  /etc/agentloop-otel.json

LABEL agentloop.instrumentation="v2-deferred" \
      agentloop.base="agentteams/copaw-worker:223ddc2-build1" \
      mp.task="finals-r3-traced-20260916"

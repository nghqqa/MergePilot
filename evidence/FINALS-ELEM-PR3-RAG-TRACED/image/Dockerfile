# RAG-enabled variant: agentloop instrumentation (inherited) + the P14-proven
# SYNTHETIC RAG MCP server + a DEFERRED hook that injects it into CoPawAgent.
# Both switches default to disabled in-image; runtime enable = rewrite the
# /etc/agentloop-*.json files (no restart). Base includes the v2.2 OTel layer.
FROM agentteams/copaw-worker:223ddc2-agentloop

ARG SITE=/opt/venv/standard/lib/python3.11/site-packages
COPY rag-mcp-server.mjs    /usr/local/bin/rag-mcp-server.mjs
COPY zz_agentloop_rag.py   ${SITE}/zz_agentloop_rag.py
COPY zzz_rag_hook.pth      ${SITE}/zzz_rag_hook.pth
COPY agentloop-rag.json    /etc/agentloop-rag.json

LABEL agentloop.instrumentation="v2.2-deferred+rag-hook" \
      mp.task="finals-pr2pr3-rag-traced-20260917"

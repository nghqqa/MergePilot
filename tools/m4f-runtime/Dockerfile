FROM python:3.12-slim

RUN pip install --no-cache-dir \
    psycopg2-binary==2.9.12 \
    jsonschema==4.25.1 \
    pytest==8.4.2 \
    fastembed==0.7.4 \
    mcp==1.28.1 \
    httpx==0.28.1 \
    anyio==4.14.2

WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
ENTRYPOINT ["python", "tools/m4f_demo.py"]

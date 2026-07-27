FROM python:3.12-slim
# B4c:加 mcp client SDK(+ httpx/anyio)用于 Controller→Gateway 调用。
# B4c-0.1 加固:固定精确版本(== gateway 镜像实测版本),保证里程碑证据可复现 + client/server SDK 同版本。
RUN pip install --no-cache-dir \
      psycopg2-binary==2.9.12 \
      mcp==1.28.1 \
      httpx==0.28.1 \
      anyio==4.14.2
COPY controller.py /app/controller.py
COPY gateway_client.py /app/gateway_client.py
COPY test_gateway_schema.py /app/test_gateway_schema.py
COPY test_startup_pgwait.py /app/test_startup_pgwait.py
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python3 -c "import psycopg2,os; psycopg2.connect(host=os.environ.get('PG_HOST','audit-pg'),port=5432,dbname=os.environ.get('PG_DATABASE','mergepilot_audit'),user=os.environ.get('PG_USER','mergepilot'),password=os.environ['PG_PASS']).close()" || exit 1
CMD ["python3", "-u", "controller.py"]

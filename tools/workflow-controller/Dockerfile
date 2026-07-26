FROM python:3.12-slim
RUN pip install --no-cache-dir psycopg2-binary
COPY controller.py /app/controller.py
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python3 -c "import psycopg2,os; psycopg2.connect(host=os.environ.get('PG_HOST','audit-pg'),port=5432,dbname=os.environ.get('PG_DATABASE','mergepilot_audit'),user=os.environ.get('PG_USER','mergepilot'),password=os.environ['PG_PASS']).close()" || exit 1
CMD ["python3", "-u", "controller.py"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Init the case-retrieval knowledge DB (PostgreSQL + pgvector).

Creates schema/table/role exactly as the pg_vector adapter expects:
  - vector(384) column, ivfflat cosine index
  - case_retrieval_reader role: read-only, no superuser, search_path=public,
    statement_timeout 10s / lock_timeout 5s
Seeds 7 real-run knowledge rows (hash384 deterministic embeddings).

Usage (inside a container with psycopg2 + repo checkout):
  python init_knowledge_db.py <admin_dsn> <seed_json> <reader_password>
The reader password is set for role case_retrieval_reader.
"""
import json
import sys

import psycopg2

DIM = 384


def hash_vec(text: str):
    """Deterministic 384-dim hash embedding (token-bucket hashing, L2-normalized).

    Same idea as the platform's offline RAG: reproducible, no model download.
    """
    import hashlib
    import math
    import re

    vec = [0.0] * DIM
    toks = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())
    for t in toks:
        h = hashlib.md5(t.encode()).digest()
        idx = int.from_bytes(h[:2], "big") % DIM
        sign = 1.0 if h[2] % 2 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 9) for v in vec]


def vec_literal(vec):
    return "[" + ",".join(format(float(v), ".9g") for v in vec) + "]"


def main():
    admin_dsn, seed_path, reader_pw = sys.argv[1], sys.argv[2], sys.argv[3]
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    cur = admin.cursor()

    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
          id          BIGSERIAL PRIMARY KEY,
          task_id     TEXT,
          finding_id  TEXT,
          category    TEXT,
          severity    TEXT,
          issue       TEXT,
          fix         TEXT,
          file        TEXT,
          source      TEXT,
          repo_scope  TEXT,
          source_pr_url TEXT,
          source_commit_sha VARCHAR(40),
          source_version TEXT,
          embedding_model TEXT,
          embedding_version TEXT,
          adopted     BOOLEAN DEFAULT FALSE,
          embedding   vector(384),
          created_at  TIMESTAMPTZ DEFAULT now()
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_vec ON knowledge USING ivfflat (embedding vector_cosine_ops) WITH (lists = 4)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_repo ON knowledge (repo_scope)")

    # reader role: strict read-only profile expected by _verify_role
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'case_retrieval_reader'")
    if cur.fetchone() is None:
        cur.execute(f"CREATE ROLE case_retrieval_reader LOGIN PASSWORD %s", (reader_pw,))
    else:
        cur.execute(f"ALTER ROLE case_retrieval_reader PASSWORD %s", (reader_pw,))
    cur.execute("ALTER ROLE case_retrieval_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
    cur.execute('GRANT CONNECT ON DATABASE "cases" TO case_retrieval_reader')
    cur.execute("REVOKE ALL PRIVILEGES ON TABLE knowledge FROM case_retrieval_reader")
    cur.execute("GRANT USAGE ON SCHEMA public TO case_retrieval_reader")
    cur.execute("GRANT SELECT ON TABLE knowledge TO case_retrieval_reader")
    cur.execute("ALTER ROLE case_retrieval_reader SET default_transaction_read_only = on")
    cur.execute("ALTER ROLE case_retrieval_reader SET statement_timeout = '10s'")
    cur.execute("ALTER ROLE case_retrieval_reader SET lock_timeout = '5s'")
    cur.execute("ALTER ROLE case_retrieval_reader SET search_path = public")
    cur.execute("ALTER ROLE case_retrieval_reader SET idle_in_transaction_session_timeout = '15s'")

    rows = json.load(open(seed_path, encoding="utf-8"))
    cur.execute("DELETE FROM knowledge")
    for r in rows:
        text = " ".join(filter(None, [r["issue"], r["fix"], r["file"], r["category"], r["severity"]]))
        vec = vec_literal(hash_vec(text))
        cur.execute(
            """INSERT INTO knowledge (task_id, finding_id, category, severity, issue, fix, file,
                 source, repo_scope, source_pr_url, source_commit_sha, source_version,
                 embedding_model, embedding_version, adopted, embedding)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)""",
            (r["task_id"], r["finding_id"], r["category"], r["severity"], r["issue"], r["fix"],
             r["file"], r["source"], r["repo_scope"], r["source_pr_url"], r["source_commit_sha"],
             r["source_version"], r["embedding_model"], r["embedding_version"], r["adopted"], vec))
    cur.execute("SELECT count(*) FROM knowledge")
    print("knowledge rows:", cur.fetchone()[0])
    cur.close()
    admin.close()


if __name__ == "__main__":
    main()

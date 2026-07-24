#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rag.py — MergePilot 经验沉淀(RAG):把历史 finding+修复向量化存 pgvector,并按相似度召回复用。
子命令:embed(建知识库)/ recall <query>(召回 top-k)。
host 跑连 localhost:5432(audit-pg 已发布);容器内跑设 PG_HOST=audit-pg。
模型 BAAI/bge-small-en-v1.5(384 维,可替换为中文优化模型,迁移=改一处)。
"""
import os
import sys

import psycopg2
from fastembed import TextEmbedding

MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
PG_DSN = os.environ.get("PG_DSN")
PG_CONFIG = {
    "host": os.environ.get("PG_HOST", "localhost"),
    "port": int(os.environ.get("PG_PORT", "5432")),
    "dbname": os.environ.get("PG_DATABASE", "mergepilot_audit"),
    "user": os.environ.get("PG_USER", "mergepilot"),
    "password": os.environ.get("PG_PASSWORD"),
}


def connect():
    if PG_DSN:
        return psycopg2.connect(PG_DSN)
    if not PG_CONFIG["password"]:
        sys.exit("ERROR: 请通过环境变量提供数据库密码:PG_PASSWORD=xxx(或整串 PG_DSN)。例如 PG_PASSWORD=xxx python rag.py recall \"...\"")
    return psycopg2.connect(**PG_CONFIG)

# 经验知识库:每条 = 一个历史 finding + 其修复(issue 文本用于向量检索,fix 召回后复用)
KNOWLEDGE = [
    {"task_id":"gh-pr1-review","finding_id":"F1","category":"security","severity":"critical","file":"user_service.py:3","source":"sast-scan",
     "issue":"hardcoded secret API key in source code, credentials leak. 硬编码 API 密钥 sk-live 明文暴露",
     "fix":"API_KEY = os.environ['OPENAI_API_KEY']; 从环境变量/密钥管理读取,源码杜绝密钥字面量,吊销已泄漏密钥"},
    {"task_id":"gh-pr1-review","finding_id":"F2","category":"security","severity":"critical","file":"user_service.py:6","source":"sast-scan",
     "issue":"SQL injection: execute query with string concatenation of user input. SQL 注入 execute 字符串拼接 name",
     "fix":"参数化查询 conn.execute('SELECT * FROM users WHERE name = ?', (name,)); 用占位符,禁止拼接"},
    {"task_id":"gh-pr1-review","finding_id":"F3","category":"quality","severity":"medium","file":"user_service.py:7","source":"manual+sast",
     "issue":"database connection leak: sqlite3 connect not closed. 连接泄漏 未关闭",
     "fix":"with sqlite3.connect(...) as conn: 用 context manager 自动关闭连接"},
    {"task_id":"gh-pr1-review","finding_id":"F4","category":"quality","severity":"medium","file":"user_service.py:5","source":"manual",
     "issue":"missing input validation: no type check / empty / length check. 输入校验缺失",
     "fix":"类型+空值+长度检查; if not isinstance(name,str) or not name: return None"},
    {"task_id":"gh-pr1-review","finding_id":"F5","category":"quality","severity":"medium","file":"user_service.py:6","source":"manual",
     "issue":"missing error handling: no try/except around DB call. 错误处理缺失 sqlite3.Error",
     "fix":"try/except sqlite3.Error 捕获异常并记录,避免未处理异常"},
]

_model = None
def model():
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL)
    return _model

def cmd_embed():
    conn = connect(); cur = conn.cursor()
    cur.execute("TRUNCATE knowledge RESTART IDENTITY;")
    vecs = list(model().embed([k["issue"] for k in KNOWLEDGE]))
    for k, v in zip(KNOWLEDGE, vecs):
        cur.execute("""INSERT INTO knowledge(task_id,finding_id,category,severity,issue,fix,file,source,embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (k["task_id"],k["finding_id"],k["category"],k["severity"],k["issue"],k["fix"],k["file"],k["source"], v.tolist()))
    conn.commit(); conn.close()
    print(f"embedded {len(KNOWLEDGE)} knowledge items ({MODEL}, {len(vecs[0])}-dim)")

def cmd_recall(query, k=3):
    conn = connect(); cur = conn.cursor()
    qv = list(model().embed([query]))[0].tolist()
    cur.execute("""SELECT finding_id, category, severity, file, issue, fix,
                          round((1-(embedding <=> %s::vector))::numeric,3) AS sim
                   FROM knowledge ORDER BY sim DESC LIMIT %s""", (qv, k))
    rows = cur.fetchall(); conn.close()
    print(f"RAG recall  query: {query!r}\n  top-{k}:")
    for r in rows:
        print(f"  [sim={r[6]}] {r[0]} ({r[1]}/{r[2]}) @ {r[3]}")
        print(f"     issue: {r[4]}")
        print(f"     fix:   {r[5]}")
    return rows

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "recall"
    if cmd == "embed":
        cmd_embed()
    elif cmd == "recall":
        q = " ".join(sys.argv[2:]) or "SQL injection execute string concatenation"
        cmd_recall(q)
    else:
        print("usage: rag.py embed | recall <query>")



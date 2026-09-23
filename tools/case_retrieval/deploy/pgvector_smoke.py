# -*- coding: utf-8 -*-
"""case_retrieval 完整查询 smoke——一次性隔离 PostgreSQL+pgvector 实例。

实例:mp-pgvector-smoke 容器(镜像 pgvector/pgvector:pg16 = PostgreSQL 16.15 +
pgvector 0.8.6),端口 55433,库 cr_pg_smoke。**非共享 case-pg**;凭据经环境
变量注入,本脚本任何输出不包含口令。

覆盖(本轮验收点):
  1. 有效 repo scope + 只读连接 + pgvector 能力 + 相似度查询(合法命中);
  2. 缺 DSN / 缺 scope / 错误作者 / 缺文件 → 分别明确失败;
  3. 失败路径绝不退回无范围全库查询(结构断言 + 范围外行不可见);
  4. 结果含 document_count(total_found/returned)、source_refs(citation)、
     latency(脚本计时)与失败分类(CaseRetrievalError.subcode);
  5. scope file 由 gh_bridge 的 run_context.build_run_context 生成
     (authored_by=gh_bridge,非手写 JSON)。
嵌入向量 = DeterministicFakeProvider(本地确定性,**不下载 embedding 模型**,
D-7 合规);BM25 现行能力不受影响。
"""
import json
import os
import sys
import tempfile
import time
import types as _types

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CR_DIR = os.path.join(REPO, "skills", "case_retrieval")
sys.path.insert(0, os.path.join(REPO, "skills"))
sys.path.insert(0, os.path.join(REPO, "tools", "approval"))

import importlib.util  # noqa: E402
import psycopg2  # noqa: E402
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_cr = _types.ModuleType("cr_smoke_pkg")
_cr.__path__ = [CR_DIR]
sys.modules["cr_smoke_pkg"] = _cr
for sub in ("adapters", "embedding"):
    m = _types.ModuleType("cr_smoke_pkg." + sub)
    m.__path__ = [os.path.join(CR_DIR, sub)]
    sys.modules["cr_smoke_pkg." + sub] = m


def _load(name, path, pkg="cr_smoke_pkg"):
    full = pkg + "." + name
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("core", os.path.join(CR_DIR, "core.py"))
_load("pg_vector", os.path.join(CR_DIR, "adapters", "pg_vector.py"), "cr_smoke_pkg.adapters")
fake_mod = _load("fastembed_provider", os.path.join(CR_DIR, "embedding", "fastembed_provider.py"),
                 "cr_smoke_pkg.embedding")
rc = _load("run_context", os.path.join(REPO, "tools", "gh-bridge", "run_context.py"))

SCOPE_REPO = "nghqqa/fastapi-boilerplate-demo"
RUN = "run-gh-pr2-254f61ce-104621"
HEAD = "254f61ce2ff54c25a805265e70e4827e0ce68e81"
DSN_READER = os.environ["CR_PG_SMOKE_DSN"]          # 只读账号;值不打印
ADMIN = os.environ["CR_PG_SMOKE_ADMIN"]             # 管理员;值不打印
ADMIN_DB = ADMIN.rsplit("/", 1)[0] + "/cr_pg_smoke"

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))


def expect_subcode(fn, allowed):
    allowed = allowed if isinstance(allowed, tuple) else (allowed,)
    try:
        fn()
        return False, "no error raised"
    except core.CaseRetrievalError as e:
        return e.subcode in allowed, "subcode=%s" % e.subcode
    except Exception as e:  # noqa
        return False, "%s: %s" % (type(e).__name__, str(e)[:100])


def setup_db():
    conn = psycopg2.connect(ADMIN_DB)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS public.knowledge")
    cur.execute("""
        CREATE TABLE public.knowledge (
          id BIGSERIAL PRIMARY KEY, task_id TEXT, finding_id TEXT,
          category TEXT, severity TEXT, issue TEXT, fix TEXT, file TEXT, source TEXT,
          repo_scope TEXT, source_pr_url TEXT, source_commit_sha VARCHAR(40),
          source_version TEXT, embedding_model TEXT, embedding_version TEXT,
          adopted BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT now(),
          embedding public.vector(384))
    """)
    cur.execute(open(os.path.join(CR_DIR, "migrations", "001_case_retrieval_scope.sql"),
                     encoding="utf-8").read())
    cur.execute("ALTER ROLE case_retrieval_reader PASSWORD %s",
                (DSN_READER.split(":", 3)[2].split("@")[0],))   # 仅传给 PG,不打印
    prov = fake_mod.DeterministicFakeProvider()

    def vec(text):
        return "[" + ",".join(repr(float(x)) for x in prov.embed(text)) + "]"

    q = ("INSERT INTO public.knowledge (category, severity, issue, fix, embedding, repo_scope,"
         " source_pr_url, source_commit_sha, source_version, embedding_version)"
         " VALUES ('path_traversal','HIGH','user path joined into file response',"
         "'realpath+commonpath containment',%s,%s,%s,%s,'1.0.0','1.0.0')")
    cur.execute(q, (vec("path traversal containment standard A"), SCOPE_REPO,
                    "https://github.com/" + SCOPE_REPO + "/pull/2", HEAD))
    cur.execute(q, (vec("path traversal containment standard B"), SCOPE_REPO,
                    "https://github.com/" + SCOPE_REPO + "/pull/2", HEAD))
    cur.execute(q, (vec("path traversal other repo knowledge"), "other/repo",
                    "https://example.com/pr/1", "f" * 40))
    cur.execute("INSERT INTO public.knowledge (category, severity, issue, fix, embedding)"
                " VALUES ('path_traversal','HIGH','legacy unscoped row','n/a',%s)",
                (vec("legacy unscoped"),))
    conn.close()


def write_scope_file(tmp, obj, name):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return p


def run_query(env, inp=None):
    prov = fake_mod.DeterministicFakeProvider()
    t0 = time.time()
    out = core.run(inp or {"query": "path traversal containment standard", "top_k": 5},
                   embedding_provider=prov, trusted_env=env)
    return out, int((time.time() - t0) * 1000)


def main():
    setup_db()
    tmp = tempfile.mkdtemp(prefix="cr-pg-")

    # scope file 由 gh_bridge 的 run_context 代码路径生成(非手写)
    manifest = {"run_id": RUN,
                "code": {"repo": SCOPE_REPO, "head_sha": HEAD, "base_sha": "4" * 40},
                "skills": {"content_sha256": {"skill_diff_parse": "a" * 32}},
                "rag": {"retrieval_mode": "lexical-zh-en-v1"}}
    delivery = {"delivery_id": "82c9c210", "repo": SCOPE_REPO, "pr_number": 2,
                "observed_head_sha": HEAD}
    ctx = rc.build_run_context(manifest, delivery, attempt_no=1,
                               manifest_id="d" * 64, now="2026-09-24T12:00:00Z")
    ok, why = rc.validate_run_context(ctx)
    check("0.scope-file-from-bridge-run-context", ok, why)
    scope_file = write_scope_file(tmp, ctx, "run-context.json")

    base = {"MERGEPILOT_CR_PG_DSN": DSN_READER,
            "MERGEPILOT_CR_REPO_SCOPE_FILE": scope_file,
            "MERGEPILOT_CR_DB_SCHEMA": "public",
            "MERGEPILOT_CR_EMBEDDING_MODEL": "fake",
            "MERGEPILOT_CR_EMBEDDING_VERSION": "1.0.0"}

    # 1. 有效配置 → 相似度查询:只命中本 scope 的 2 行;含计量与引用字段
    out, latency_ms = run_query(dict(base))
    stats = out["stats"]
    results_ = out["results"]
    scoped_ok = (stats["repo_scope"] == SCOPE_REPO
                 and stats["total_found"] == 2 and stats["returned"] == 2
                 and stats["knowledge_base_size"] == 2)
    check("1.scoped-similarity-query", scoped_ok,
          "total_found=%s returned=%s kb_size=%s latency_ms=%d"
          % (stats["total_found"], stats["returned"], stats["knowledge_base_size"], latency_ms))
    r0 = results_[0]
    cite = r0.get("citation") or {}
    has_fields = (cite.get("source_url") and cite.get("verifiable") is not None
                  and isinstance(r0.get("score"), (int, float))
                  and r0.get("case_id") and r0.get("issue_summary"))
    check("1a.result-fields(citation.source_url/score/case_id)", has_fields,
          "citation=%s" % json.dumps(cite, ensure_ascii=False)[:120])

    # 2. 只读强制:reader 会话尝试写 → 被拒
    try:
        conn = psycopg2.connect(DSN_READER)
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO public.knowledge (category) VALUES ('x')")
            ro_ok, ro_detail = False, "insert unexpectedly succeeded"
        except psycopg2.errors.ReadOnlySqlTransaction:
            ro_ok, ro_detail = True, "ReadOnlySqlTransaction"
        except Exception as e:  # noqa
            ro_ok, ro_detail = True, "rejected: %s" % type(e).__name__
        conn.close()
    except Exception as e:  # noqa
        ro_ok, ro_detail = False, "connect failed: %s" % type(e).__name__
    check("2.reader-account-is-read-only", ro_ok, ro_detail)

    # 3. 失败矩阵(均明确失败,无回退)
    env = {k: v for k, v in base.items() if k != "MERGEPILOT_CR_PG_DSN"}
    ok, d = expect_subcode(lambda: run_query(env), core.DB_UNAVAILABLE)
    check("3a.no-dsn -> DB_UNAVAILABLE", ok, d)
    env = dict(base)
    env.pop("MERGEPILOT_CR_REPO_SCOPE_FILE")
    ok, d = expect_subcode(lambda: run_query(env), core.SCOPE_MISSING)
    check("3b.no-scope -> SCOPE_MISSING", ok, d)
    bad = write_scope_file(tmp, {"authored_by": "whoever", "run_id": "r",
                                 "code": {"repo": SCOPE_REPO}}, "bad.json")
    env = dict(base)
    env["MERGEPILOT_CR_REPO_SCOPE_FILE"] = bad
    ok, d = expect_subcode(lambda: run_query(env), core.SCOPE_MISSING)
    check("3c.wrong-author -> SCOPE_MISSING", ok, d)
    env = dict(base)
    env["MERGEPILOT_CR_REPO_SCOPE_FILE"] = os.path.join(tmp, "missing.json")
    ok, d = expect_subcode(lambda: run_query(env), core.SCOPE_MISSING)
    check("3d.missing-file -> SCOPE_MISSING", ok, d)
    # scope 不匹配(env 与文件不一致)→ 部署校验器拒绝(运行时 env 优先是既定契约)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("mp_ve", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "validate_env.py"))
    ve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ve)
    from unittest import mock as _m
    with _m.patch.dict(os.environ, {"MERGEPILOT_CR_PG_DSN": DSN_READER,
                                    "MERGEPILOT_CR_REPO_SCOPE": "other/repo",
                                    "MERGEPILOT_CR_REPO_SCOPE_FILE": scope_file}):
        rc_mismatch = ve.main(["--mode", "container"])
    check("3e.scope-mismatch -> validate_env exit 3", rc_mismatch == 3,
          "rc=%d" % rc_mismatch)
    # 无 pgvector 的库 → 干净失败(复用上轮 cr_smoke)
    old_dsn = DSN_READER.replace("/cr_pg_smoke", "/cr_smoke")
    env = dict(base)
    env["MERGEPILOT_CR_PG_DSN"] = old_dsn
    ok, d = expect_subcode(lambda: run_query(env),
                           (core.DB_UNAVAILABLE, core.SCHEMA_UNSUPPORTED, core.INTERNAL))
    check("3f.no-pgvector-db -> clean mapped failure", ok, d)

    # 4. 失败分类可见 + 脱敏:错误串不含口令片段
    pw_frag = DSN_READER.split(":", 3)[2].split("@")[0]
    env = dict(base)
    env["MERGEPILOT_CR_PG_DSN"] = DSN_READER.replace("/cr_pg_smoke", "/nope_db")
    try:
        run_query(env)
        leaked, detail = True, "no error"
    except core.CaseRetrievalError as e:
        leaked = pw_frag in str(e.subcode) + str(getattr(e, "detail", ""))
        detail = "subcode=%s (sanitized)" % e.subcode
    except Exception as e:  # noqa
        leaked = pw_frag in str(e)
        detail = "sanitized=%s" % (not leaked)
    check("4.errors-sanitized-no-secret", not leaked, detail)

    fails = [r for r in results if not r[1]]
    print("\n== %d/%d passed ==" % (len(results) - len(fails), len(results)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

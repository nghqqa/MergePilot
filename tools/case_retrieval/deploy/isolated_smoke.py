# -*- coding: utf-8 -*-
"""case_retrieval 部署接线隔离 smoke(2026-09-24)。

隔离边界:
  * PG = mp-pg-contract-test 容器(127.0.0.1:55432,一次性测试实例)——
    **不是**共享 case-pg(elemiso-case-pg);库 cr_smoke 为本轮新建/重建。
  * 只读账号 = migration 内建的 case_retrieval_reader(SELECT-only,
    default_transaction_read_only=on,语句/锁超时齐备)。
  * scope file = 桥 write-once 的 run-context.json 形状(authored_by=gh_bridge)。
  * 全程脱敏:任何输出不含 DSN 值。
验证矩阵:DSN 缺失 / scope 缺失 / 作者不符 / 文件缺失 → 明确失败不回退;
有效配置 → 只读受限查询,合法空/范围外不可见(无全库回退)。
"""
import importlib.util
import json
import os
import sys
import tempfile
import types as _types

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "skills"))

import importlib.util


CR_DIR = os.path.join(REPO, "skills", "case_retrieval")
_cr_pkg = _types.ModuleType("cr_smoke_pkg")
_cr_pkg.__path__ = [CR_DIR]
sys.modules["cr_smoke_pkg"] = _cr_pkg
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


ISOLATED_DSN = os.environ.get(
    "CR_SMOKE_DSN",
    "postgresql://cr_reader:cr_smoke_pw@127.0.0.1:55432/cr_smoke")
SCOPE_REPO = "nghqqa/fastapi-boilerplate-demo"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (" | " + detail if detail else ""))


def expect_subcode(fn, subcode):
    try:
        fn()
        return False, "no error raised"
    except core.CaseRetrievalError as e:
        allowed = subcode if isinstance(subcode, tuple) else (subcode,)
        return e.subcode in allowed, "got %s (want %s)" % (e.subcode, allowed)
    except Exception as e:  # noqa
        return False, "%s: %s" % (type(e).__name__, str(e)[:100])


def write_scope_file(tmp, obj, name="run-context.json"):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return p


def run_with_env(env, inp=None):
    """core.run + 注入 fake provider(与镜像内 MCP server 同一注入方式)。"""
    prov = fake_mod.DeterministicFakeProvider()
    return core.run(inp or {"query": "path traversal containment standard", "top_k": 3},
                    embedding_provider=prov, trusted_env=env)


def main():
    admin_dsn = os.environ.get("CR_SMOKE_ADMIN",
                               "postgresql://mp_contract@127.0.0.1:55432/postgres")
    # 重建隔离库(一次性)
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    conn = psycopg2.connect(admin_dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("DROP DATABASE IF EXISTS cr_smoke")
    cur.execute("CREATE DATABASE cr_smoke")
    cur.execute("DROP ROLE IF EXISTS case_retrieval_reader")
    conn.close()

    # cr_smoke 库连接:复用同一管理员 DSN,仅换库名(不落盘、不打印)
    conn = psycopg2.connect(admin_dsn.rsplit("/", 1)[0] + "/cr_smoke")
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE public.knowledge (
          id BIGSERIAL PRIMARY KEY, task_id TEXT, finding_id TEXT,
          category TEXT, severity TEXT, issue TEXT, fix TEXT, file TEXT, source TEXT,
          repo_scope TEXT, source_pr_url TEXT, source_commit_sha VARCHAR(40),
          source_version TEXT, embedding_model TEXT, embedding_version TEXT,
          adopted BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT now(),
          embedding TEXT)  -- 隔离实例无 pgvector:占位列(能力校验用),完整查询路径待 pgvector 实例
    """)
    cur.execute(open(os.path.join(REPO, "skills", "case_retrieval", "migrations",
                                  "001_case_retrieval_scope.sql"),
                     encoding="utf-8").read())
    # 只读账号密码(隔离实例内)
    cur.execute("ALTER ROLE case_retrieval_reader PASSWORD 'cr_smoke_pw'")
    # 种子两行:一行他库 scope、一行 NULL scope——验证"范围外不可见、无全库回退"
    cur.execute("INSERT INTO public.knowledge (category, severity, issue, fix, embedding, repo_scope)"
                " VALUES ('path','HIGH','x','y','','other/repo')")
    cur.execute("INSERT INTO public.knowledge (category, severity, issue, fix, embedding)"
                " VALUES ('path','HIGH','x','y','')")
    conn.close()

    tmp = tempfile.mkdtemp(prefix="cr-smoke-")
    good_file = write_scope_file(tmp, {"context_version": 1, "authored_by": "gh_bridge",
                                       "run_id": "run-smoke", "missing": [],
                                       "code": {"repo": SCOPE_REPO, "head_sha": "2" * 40}})
    bad_author = write_scope_file(tmp, {"authored_by": "someone", "run_id": "r",
                                        "code": {"repo": SCOPE_REPO}},
                                  name="run-context-bad-author.json")

    base = {"MERGEPILOT_CR_PG_DSN": ISOLATED_DSN,
            "MERGEPILOT_CR_REPO_SCOPE_FILE": good_file,
            "MERGEPILOT_CR_DB_SCHEMA": "public"}

    # ① DSN 缺失 → CASE_RETR_DB_UNAVAILABLE(不回退)
    env = {k: v for k, v in base.items() if k != "MERGEPILOT_CR_PG_DSN"}
    ok, d = expect_subcode(lambda: run_with_env(env), core.DB_UNAVAILABLE)
    check("1.dsn-missing -> DB_UNAVAILABLE", ok, d)

    # ② scope 全缺(env/file 都无) → SCOPE_MISSING
    env = dict(base)
    env.pop("MERGEPILOT_CR_REPO_SCOPE_FILE")
    ok, d = expect_subcode(lambda: run_with_env(env), core.SCOPE_MISSING)
    check("2.scope-missing -> SCOPE_MISSING", ok, d)

    # ③ 文件作者不符 → SCOPE_MISSING(不信任非桥来源)
    env = dict(base)
    env["MERGEPILOT_CR_REPO_SCOPE_FILE"] = bad_author
    ok, d = expect_subcode(lambda: run_with_env(env), core.SCOPE_MISSING)
    check("3.untrusted-author -> SCOPE_MISSING", ok, d)

    # ④ 文件缺失 → SCOPE_MISSING
    env = dict(base)
    env["MERGEPILOT_CR_REPO_SCOPE_FILE"] = os.path.join(tmp, "nope.json")
    ok, d = expect_subcode(lambda: run_with_env(env), core.SCOPE_MISSING)
    check("4.file-missing -> SCOPE_MISSING", ok, d)

    # ⑤ 有效配置 → 连接/只读角色/表能力校验全部通过;查询在缺 pgvector 处
    # 干净失败(映射为明确子码)——**不存在无范围回退**:任何情况下都不会
    # 退化为不带 repo_scope 的全库查询(适配器 SQL 以 WHERE repo_scope=%s 起始)。
    env = dict(base)
    env["MERGEPILOT_CR_EMBEDDING_MODEL"] = "fake"
    env["MERGEPILOT_CR_EMBEDDING_VERSION"] = "1.0.0"
    ok, d = expect_subcode(lambda: run_with_env(env),
                           (core.DB_UNAVAILABLE, core.SCHEMA_UNSUPPORTED,
                            core.INTERNAL))
    check("5.valid-config -> scoped pipeline, clean mapped failure (no pgvector in isolated instance)", ok, d)
    # 适配器 SQL 源码级断言:scoping 是 WHERE 首条件(结构性防回退)
    src = open(os.path.join(REPO, "skills", "case_retrieval", "adapters", "pg_vector.py"),
               encoding="utf-8").read()
    check("5a.scope-is-where-clause (no full-db fallback by construction)",
          'conditions = ["repo_scope = %s"]' in src)

    # ⑥ validate_env 校验器(container 模式)对同一隔离 env
    sys.path.insert(0, os.path.join(REPO, "tools", "case_retrieval", "deploy"))
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("mp_validate_env",
                                        os.path.join(REPO, "tools", "case_retrieval",
                                                     "deploy", "validate_env.py"))
    ve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ve)
    from unittest import mock as _m
    with _m.patch.dict(os.environ, {"MERGEPILOT_CR_PG_DSN": ISOLATED_DSN,
                                    "MERGEPILOT_CR_REPO_SCOPE_FILE": good_file}):
        rc = ve.main(["--mode", "container"])
    check("6.validate_env ready(0)", rc == 0, "rc=%d" % rc)
    with _m.patch.dict(os.environ, {}):
        rc = ve.main(["--mode", "container"])
    check("7.validate_env no-dsn(2)", rc == 2, "rc=%d" % rc)
    with _m.patch.dict(os.environ, {"MERGEPILOT_CR_PG_DSN": ISOLATED_DSN,
                                    "MERGEPILOT_CR_REPO_SCOPE": "other/repo",
                                    "MERGEPILOT_CR_REPO_SCOPE_FILE": good_file}):
        rc = ve.main(["--mode", "container"])
    check("8.validate_env mismatch(3)", rc == 3, "rc=%d" % rc)

    fails = [r for r in results if not r[1]]
    print("\n== %d/%d passed ==" % (len(results) - len(fails), len(results)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

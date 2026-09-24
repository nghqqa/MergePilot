# -*- coding: utf-8 -*-
"""validate_env — case_retrieval 部署接线启动前校验(脱敏;本地准备,未部署)。

用法:
  python validate_env.py --mode container    # 容器内自检(生产路径)
  python validate_env.py --mode container --scope-file /path/to/run-context.json
  python validate_env.py --mode repo         # repo 侧单测/CI 用(无 env 时全 skip)

退出码:
  0 = 就绪
  2 = MERGEPILOT_CR_PG_DSN 缺失(容器模式)
  3 = scope 缺失 / env 与文件不一致 / 文件不可信(作者非法/形状非法)
  4 = --scope-file 显式给定但读取失败
脱敏承诺:只打印变量名与判定结果,绝不打印 DSN/scope 的值。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_TRUSTED_AUTHOR = "gh_bridge"


def _load_run_context(path):
    """读取 run-context;返回 (repo|None, reason)。只认 gh_bridge 作者。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return None, "unreadable: %s" % type(e).__name__
    except ValueError:
        return None, "not json"
    if not isinstance(data, dict):
        return None, "not object"
    if data.get("authored_by") != _TRUSTED_AUTHOR:
        return None, "untrusted author (must be %s)" % _TRUSTED_AUTHOR
    if not isinstance(data.get("run_id"), str) or not data.get("run_id"):
        return None, "run_id missing"
    # 真实桥产出:repo 在顶层;code.repo 兼容
    repo = data.get("repo")
    if not isinstance(repo, str) or not repo:
        code = data.get("code") if isinstance(data.get("code"), dict) else {}
        repo = code.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        return None, "repo missing/invalid"
    return repo, ""


def _preflight_db(env) -> int:
    """连接 DSN(只读)并校验角色/表能力;脱敏:失败只打印类别,不打印 DSN。"""
    try:
        _root = os.path.abspath(__file__)
        for _ in range(4):          # deploy -> case_retrieval -> tools -> 仓库根
            _root = os.path.dirname(_root)
        sys.path.insert(0, _root)
        from skills.case_retrieval.adapters.pg_vector import PgVectorAdapter  # noqa: E402
    except Exception as e:  # noqa
        print("FAIL preflight adapter import: %s" % type(e).__name__)
        return 5
    adapter = PgVectorAdapter({
        "dsn": env.get("MERGEPILOT_CR_PG_DSN"),
        "schema": env.get("MERGEPILOT_CR_DB_SCHEMA", "public"),
        "table": env.get("MERGEPILOT_CR_DB_TABLE", "knowledge"),
        "statement_timeout_ms": 5000,
        "lock_timeout_ms": 3000,
        "connect_timeout_ms": 5000,
    })
    try:
        # 以下三步 = 适配器真实启动校验序列(只读会话,零业务查询)
        conn = adapter._connect()
        adapter._verify_role()
        adapter._verify_schema_capability()
        conn.rollback()
        print("ok   preflight: connection + read-only role + table capability verified")
        return 0
    except Exception as e:  # noqa
        sub = getattr(e, "subcode", type(e).__name__)
        print("FAIL preflight: %s (sanitized, no DSN details)" % sub)
        return 5
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("container", "repo"), default="repo")
    ap.add_argument("--scope-file", default=None,
                    help="显式校验一个 run-context 文件(覆盖 env)")
    ap.add_argument("--preflight", action="store_true",
                    help="container 模式附加:真实连接 DSN 并校验只读角色与表能力"
                         "(不执行任何业务查询);退出码 5=连接/校验失败")
    a = ap.parse_args(argv)

    if a.mode == "repo":
        print("repo mode: nothing to validate (no container env expected)")
        return 0

    if not os.environ.get("MERGEPILOT_CR_PG_DSN"):
        print("FAIL MERGEPILOT_CR_PG_DSN missing -> skill will fail CASE_RETR_DB_UNAVAILABLE")
        return 2
    print("ok   MERGEPILOT_CR_PG_DSN present")

    if a.preflight:
        # 启动前预检:真实连接 + 只读角色/表能力校验(只读会话,零业务查询)。
        rc_code = _preflight_db(os.environ)
        if rc_code != 0:
            return rc_code

    env_scope = (os.environ.get("MERGEPILOT_CR_REPO_SCOPE") or "").strip()
    file_path = a.scope_file or os.environ.get("MERGEPILOT_CR_REPO_SCOPE_FILE") or ""
    if not env_scope and not file_path:
        print("FAIL scope missing (MERGEPILOT_CR_REPO_SCOPE / *_FILE) "
              "-> skill will fail CASE_RETR_SCOPE_MISSING")
        return 3

    file_repo = None
    if file_path:
        file_repo, why = _load_run_context(file_path)
        if file_repo is None:
            print("FAIL MERGEPILOT_CR_REPO_SCOPE_FILE %s: %s" % ("(path hidden)", why))
            return 4 if a.scope_file and why.startswith("unreadable") else 3
        print("ok   scope file trusted (authored_by=%s, repo present)" % _TRUSTED_AUTHOR)

    if env_scope and file_repo and env_scope != file_repo:
        # 配置错误:env 与可信 run-context 不一致 → 拒绝启动(不静默任选其一)
        print("FAIL scope mismatch: env vs run-context differ -> fix deployment")
        return 3
    scope = env_scope or file_repo
    if not _REPO_RE.fullmatch(scope or ""):
        print("FAIL scope invalid shape")
        return 3
    print("ok   scope present (value hidden); source=%s"
          % ("env" if env_scope else "run-context-file"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

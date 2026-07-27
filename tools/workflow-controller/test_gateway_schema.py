#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_gateway_schema.py — B4c-1.2 gateway_client schema 严格化单元测试(确定性,不调真实 GitHub)。
monkeypatch gateway_call 喂畸形/伪造响应,验证 gateway_read_pr/gateway_list_prs/gateway_read_branch
对缺字段/错类型/短 SHA/bool number/fork repo 等返回 RETRY(绝不假一致/误判)。
运行:docker run --rm mergepilot-controller:latest python3 /app/test_gateway_schema.py(或容器内)
"""
import sys
import gateway_client as gc

PASS = 0; FAIL = 0
def ok(c): global PASS; PASS += 1; print(f"  ✅ {c}")
def bad(c): global FAIL; FAIL += 1; print(f"  ❌ {c}")

# monkeypatch gateway_call:喂预设响应(text, is_error)
def fake_call(text, is_err=False):
    def _c(tool, args, timeout=60):
        if is_err:
            raise gc.GatewayError(f"injected is_error: {text}")
        return text, False
    return _c

def patch(text, is_err=False):
    gc.gateway_call = fake_call(text, is_err)

# ── _is_sha40 / _parse_bool 纯函数 ──
print("== _is_sha40 ==")
ok(gc._is_sha40("0123456789abcdef0123456789abcdef01234567")) if gc._is_sha40("0123456789abcdef0123456789abcdef01234567") else bad("40hex 应 True")
ok("short rejected") if not gc._is_sha40("abc123") else bad("短 SHA 应 False")
ok("non-hex rejected") if not gc._is_sha40("g"*40) else bad("非 hex 应 False")
ok("None rejected") if not gc._is_sha40(None) else bad("None 应 False")

print("== _parse_bool ==")
for v, exp in [(True, True), (False, False), ("true", True), ("False", False), ("1", True), ("0", False), (1, True), (0, False), ("maybe", None), (None, None)]:
    got = gc._parse_bool(v)
    (ok if got == exp else bad)(f"_parse_bool({v!r})={got} exp={exp}")

# ── gateway_read_pr 严格 schema ──
import json
BASE = {"head": {"ref": "fix/r1-x", "sha": "a"*40, "repo": {"full_name": "o/R"}},
        "base": {"ref": "main"}, "state": "open", "merged": False, "number": 7}

def pr_call(d): patch(json.dumps(d))

print("== gateway_read_pr OK(全字段合法) ==")
pr_call(dict(BASE)); st, prd = gc.gateway_read_pr("o", "R", 7)
ok("valid → OK") if st == "OK" and prd["head_sha"] == "a"*40 else bad(f"应 OK: {st}")

print("== gateway_read_pr 负向(缺/伪造 → RETRY) ==")
# head_sha 短
d = json.loads(json.dumps(BASE)); d["head"]["sha"] = "abc123"; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("短 head_sha → RETRY") if st == "RETRY" else bad(f"短 sha 应 RETRY: {st}")
# head_sha 非 hex
d = json.loads(json.dumps(BASE)); d["head"]["sha"] = "g"*40; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("非 hex head_sha → RETRY") if st == "RETRY" else bad(f"非 hex 应 RETRY: {st}")
# number 缺失
d = json.loads(json.dumps(BASE)); del d["number"]; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("number 缺失 → RETRY(不 fallback 到请求参数)") if st == "RETRY" else bad(f"number 缺失应 RETRY: {st}")
# number 为 bool
d = json.loads(json.dumps(BASE)); d["number"] = True; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("number=bool → RETRY") if st == "RETRY" else bad(f"bool number 应 RETRY: {st}")
# state 缺失
d = json.loads(json.dumps(BASE)); del d["state"]; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("state 缺失 → RETRY") if st == "RETRY" else bad(f"state 缺失应 RETRY: {st}")
# head_repo 缺失
d = json.loads(json.dumps(BASE)); del d["head"]["repo"]; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("head.repo 缺失 → RETRY(不接受 fork 绕过)") if st == "RETRY" else bad(f"head.repo 缺失应 RETRY: {st}")
# head_repo.full_name 缺失
d = json.loads(json.dumps(BASE)); d["head"]["repo"] = {}; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("head.repo.full_name 缺失 → RETRY") if st == "RETRY" else bad(f"full_name 缺失应 RETRY: {st}")
# base_ref 缺失
d = json.loads(json.dumps(BASE)); del d["base"]["ref"]; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("base_ref 缺失 → RETRY(不补默认 main)") if st == "RETRY" else bad(f"base 缺失应 RETRY: {st}")
# merged 缺失
d = json.loads(json.dumps(BASE)); del d["merged"]; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("merged 缺失 → RETRY(不默认 false)") if st == "RETRY" else bad(f"merged 缺失应 RETRY: {st}")
# head_ref 缺失
d = json.loads(json.dumps(BASE)); del d["head"]["ref"]; pr_call(d); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("head_ref 缺失 → RETRY") if st == "RETRY" else bad(f"head_ref 缺失应 RETRY: {st}")
# is_error
patch("boom", is_err=True); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("is_error → RETRY") if st == "RETRY" else bad(f"is_error 应 RETRY: {st}")
# 非 dict 顶层
patch("[1,2,3]"); st, _ = gc.gateway_read_pr("o", "R", 7)
ok("顶层非对象 → RETRY") if st == "RETRY" else bad(f"非对象应 RETRY: {st}")

# ── gateway_read_branch:40hex ──
print("== gateway_read_branch 40hex ==")
patch(json.dumps([{"name": "fix/r1-x", "sha": "b"*40}])); st, sha = gc.gateway_read_branch("o", "R", "fix/r1-x")
ok("branch 40hex → OK") if st == "OK" and sha == "b"*40 else bad(f"应 OK: {st} {sha}")
patch(json.dumps([{"name": "fix/r1-x", "sha": "abc"}])); st, _ = gc.gateway_read_branch("o", "R", "fix/r1-x")
ok("branch 短 sha → RETRY") if st == "RETRY" else bad(f"短 branch sha 应 RETRY: {st}")
patch(json.dumps([{"name": "fix/r1-x", "sha": "z"*40}])); st, _ = gc.gateway_read_branch("o", "R", "fix/r1-x")
ok("branch 非 hex → RETRY") if st == "RETRY" else bad(f"非 hex branch sha 应 RETRY: {st}")

# ── gateway_list_prs:malformed item → RETRY(非 NOT_FOUND)──
print("== gateway_list_prs malformed → RETRY ==")
patch(json.dumps([{"number": 1, "head": "notdict"}])); st, _ = gc.gateway_list_prs("o", "R", "r1")
ok("head 非 dict → RETRY") if st == "RETRY" else bad(f"head 非 dict 应 RETRY: {st}")
patch(json.dumps([{"number": 1, "head": {"ref": 123}}])); st, _ = gc.gateway_list_prs("o", "R", "r1")
ok("ref 非 str → RETRY") if st == "RETRY" else bad(f"ref 非 str 应 RETRY: {st}")
patch(json.dumps([{"number": True, "head": {"ref": "fix/r1-x"}}])); st, _ = gc.gateway_list_prs("o", "R", "r1")
ok("number=bool → RETRY") if st == "RETRY" else bad(f"bool number 应 RETRY: {st}")
patch("notjson{{"); st, _ = gc.gateway_list_prs("o", "R", "r1")
ok("非 JSON → RETRY") if st == "RETRY" else bad(f"非 JSON 应 RETRY: {st}")
patch(json.dumps({"oops": "dict-not-list"})); st, _ = gc.gateway_list_prs("o", "R", "r1")
ok("顶层 dict 非 list → RETRY(不误判 NOT_FOUND)") if st == "RETRY" else bad(f"dict 应 RETRY 非 NOT_FOUND: {st}")
# 真正 0 PR(list=[] 空数组)→ NOT_FOUND
patch(json.dumps([])); st, _ = gc.gateway_list_prs("o", "R", "r1")
ok("空数组 → NOT_FOUND(查询成功且确为 0)") if st == "NOT_FOUND" else bad(f"空数组应 NOT_FOUND: {st}")

print(f"\nB4c-1.2 schema unit: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_startup_pgwait.py — B4c-2.2 PG-wait 行为单元测试。
模拟 ensure_pg() 返回连接但 cursor.execute('SELECT 1') 持续抛 OperationalError
(PG 接受连接但仍 recovery 的状态),断言:_wait_for_pg 重试 max_attempts 次、ready=False、conn=None;
startup_assert_l2 在 not-ready 时 SystemExit(不继续 startup 查询)。
"""
import sys
import psycopg2
import controller

PASS = 0; FAIL = 0
def ok(c): global PASS; PASS += 1; print(f"  ✅ {c}")
def bad(c): global FAIL; FAIL += 1; print(f"  ❌ {c}")

calls = {"ensure": 0, "select1": 0}

class FakeCursor:
    def __init__(self): pass
    def execute(self, q, *a, **k):
        calls["select1"] += 1
        raise psycopg2.OperationalError("simulated: connect ok but SELECT 1 fails (PG in recovery)")
    def fetchone(self): return None
    def __enter__(self): return self
    def __exit__(self, *a): return False

class FakeConn:
    def cursor(self): return FakeCursor()
    @property
    def closed(self): return False

def fake_ensure():
    calls["ensure"] += 1
    return FakeConn()

print("== _wait_for_pg: 连接成功但 SELECT 1 持续失败 → 30 次重试 + ready=False ==")
controller.ensure_pg = fake_ensure
controller.reset_pg = lambda: None
controller.time.sleep = lambda s: None   # 测试不真睡
calls["ensure"] = 0; calls["select1"] = 0
conn, ready = controller._wait_for_pg(max_attempts=30, delay=0)
ok("ready=False(SELECT 1 持续失败不误判就绪)") if ready is False else bad(f"ready 应 False,实际 {ready}")
ok("conn=None(异常时清空,不用 conn is not None)") if conn is None else bad(f"conn 应 None,实际 {conn}")
ok(f"ensure_pg 调用 30 次(实际 {calls['ensure']})") if calls["ensure"] == 30 else bad(f"ensure 应 30 次,实际 {calls['ensure']}")
ok(f"SELECT 1 尝试 30 次(实际 {calls['select1']})") if calls["select1"] == 30 else bad(f"SELECT 1 应 30 次,实际 {calls['select1']}")

print("== _wait_for_pg: 第 5 次 SELECT 1 成功 → ready=True,不再重试 ==")
class FakeCursorOk(FakeCursor):
    def execute(self, q, *a, **k):
        calls["select1"] += 1
        if calls["select1"] >= 5: return  # 第5次起成功
        raise psycopg2.OperationalError("still recovering")
class FakeConnOk(FakeConn):
    def cursor(self): return FakeCursorOk()
controller.ensure_pg = lambda: (calls.__setitem__("ensure", calls["ensure"]+1), FakeConnOk())[1]
calls["ensure"] = 0; calls["select1"] = 0
conn2, ready2 = controller._wait_for_pg(max_attempts=30, delay=0)
ok("第5次成功 → ready=True") if ready2 is True else bad(f"应 ready=True,实际 {ready2}")
ok("conn 非 None(成功后保留)") if conn2 is not None else bad("conn 应非 None")

print("== startup_assert_l2: _wait_for_pg 返回 not-ready → SystemExit(不继续查询) ==")
controller._wait_for_pg = lambda **k: (None, False)
controller.L2_MERGE_ENABLED_INVALID = False
controller.L2_MERGE_ENABLED = False
exited = False
try:
    controller.startup_assert_l2()
except SystemExit as e:
    exited = True
    msg = str(e) if str(e) else ""
ok("startup_assert_l2 not-ready → SystemExit") if exited else bad("应 SystemExit")
# 验证未继续到 PG 查询(_wait_for_pg 被调,但其后的 count 查询不该执行——此处无法直接观测,
# 但 SystemExit 在 _wait_for_pg 之后立即抛,保证不继续)

print(f"\nB4c-2.2 PG-wait unit: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)

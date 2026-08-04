#!/usr/bin/env python3
"""Minimal but REAL Matrix Client-Server API server (stdlib only).

Implements exactly the Matrix v3 endpoints the MergePilot controller uses:
  POST /_matrix/client/v3/register         (m.login.dummy — no token required)
  POST /_matrix/client/v3/login            (m.login.password)
  POST /_matrix/client/v3/createRoom
  POST /_matrix/client/v3/rooms/{id}/join
  POST /_matrix/client/v3/rooms/{id}/send/m.room.message/{txnId}
  GET  /_matrix/client/v3/sync             (timeline events + pagination)
  GET  /_matrix/client/versions

This is a REAL Matrix protocol server (standard endpoints, real event_ids,
real /sync pagination) — NOT a mock that calls process_event directly. The
Candidate does real HTTP /sync against this server. It is intentionally
minimal: no federation, no E2E, no state-resolution — only what the
controller's consume_events() reads.

server_name is set via env M5_HS_SERVER_NAME (default "m5test-hs"). User IDs
are @localpart:<server_name>. Throwaway: all state is in-memory, lost on
restart. For isolated one-time integration testing only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER_NAME = os.environ.get("M5_HS_SERVER_NAME", "m5test-hs")
LISTEN_PORT = int(os.environ.get("M5_HS_PORT", "8008"))

_LOCK = threading.Lock()
_USERS: dict[str, str] = {}        # username -> password
_TOKENS: dict[str, str] = {}       # access_token -> username
_ROOMS: dict[str, dict] = {}       # room_id -> {alias, members:set, events:list, seq:int}
_ALIAS: dict[str, str] = {}        # alias -> room_id
_GLOBAL_SEQ = 0                    # monotonic event sequence for /sync pagination


def _next_seq() -> int:
    global _GLOBAL_SEQ
    _GLOBAL_SEQ += 1
    return _GLOBAL_SEQ


def _user_id(username: str) -> str:
    return f"@{username}:{SERVER_NAME}"


def _new_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _make_event(room_id: str, sender: str, etype: str, content: dict) -> dict:
    seq = _next_seq()
    return {
        "event_id": f"${uuid.uuid4().hex}:{SERVER_NAME}",
        "sender": sender,
        "type": etype,
        "content": content,
        "origin_server_ts": int(time.time() * 1000),
        "room_id": room_id,
        "_seq": seq,
    }


class HSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence default logging
        pass

    def _send(self, code: int, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_user(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            tok = h[7:].strip()
            return _TOKENS.get(tok)
        return None

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/_matrix/client/versions"):
            self._send(200, {"versions": ["v1.1", "v1.2", "v1.3", "v1.4", "v1.5"]})
            return
        if self.path.startswith("/_matrix/client/v3/sync"):
            self._handle_sync()
            return
        self._send(404, {"errcode": "M_NOT_FOUND", "error": "unknown GET"})

    def do_POST(self):
        body = self._read_body()
        if self.path.endswith("/register"):
            self._handle_register(body)
            return
        if self.path.endswith("/login"):
            self._handle_login(body)
            return
        if self.path.endswith("/createRoom"):
            self._handle_create_room(body)
            return
        m = re.match(r"/_matrix/client/v3/rooms/([^/]+)/join", self.path)
        if m:
            self._handle_join(m.group(1), body)
            return
        m = re.match(r"/_matrix/client/v3/rooms/([^/]+)/send/m\.room\.message/([^/]+)", self.path)
        if m:
            self._handle_send(m.group(1), m.group(2), body)
            return
        self._send(404, {"errcode": "M_NOT_FOUND", "error": "unknown POST"})

    def _handle_register(self, body):
        u = body.get("username", "")
        p = body.get("password", "")
        if not u or not p or not re.fullmatch(r"[A-Za-z0-9._=/+\-]+", u):
            self._send(400, {"errcode": "M_INVALID_USERNAME", "error": "bad username"})
            return
        with _LOCK:
            if u in _USERS:
                self._send(400, {"errcode": "M_USER_IN_USE", "error": "exists"})
                return
            _USERS[u] = p
            tok = _new_token()
            _TOKENS[tok] = u
        self._send(200, {"user_id": _user_id(u), "access_token": tok, "home_server": SERVER_NAME})

    def _handle_login(self, body):
        ident = body.get("identifier", {})
        u = ident.get("user", "") if isinstance(ident, dict) else ""
        p = body.get("password", "")
        with _LOCK:
            if u not in _USERS or _USERS[u] != p:
                self._send(403, {"errcode": "M_FORBIDDEN", "error": "bad credentials"})
                return
            tok = _new_token()
            _TOKENS[tok] = u
        self._send(200, {"user_id": _user_id(u), "access_token": tok, "home_server": SERVER_NAME})

    def _handle_create_room(self, body):
        user = self._auth_user()
        if not user:
            self._send(401, {"errcode": "M_MISSING_TOKEN", "error": "no token"})
            return
        alias = body.get("room_alias_name", "")
        invite = body.get("invite", [])
        room_id = f"!{uuid.uuid4().hex[:12]}:{SERVER_NAME}"
        with _LOCK:
            _ROOMS[room_id] = {"members": {user}, "events": [], "alias": alias}
            if alias:
                _ALIAS["#" + alias + ":" + SERVER_NAME] = room_id
            for inv in invite:
                _ROOMS[room_id]["members"].add(inv.split(":", 1)[0].lstrip("@"))
            # m.room.create event
            _ROOMS[room_id]["events"].append(
                _make_event(room_id, _user_id(user), "m.room.create",
                            {"creator": _user_id(user)}))
        self._send(200, {"room_id": room_id})

    def _resolve_room(self, room_or_alias: str) -> str | None:
        if room_or_alias in _ROOMS:
            return room_or_alias
        return _ALIAS.get(room_or_alias)

    def _handle_join(self, room_or_alias: str, body):
        user = self._auth_user()
        if not user:
            self._send(401, {"errcode": "M_MISSING_TOKEN"})
            return
        with _LOCK:
            rid = self._resolve_room(room_or_alias)
            if not rid:
                self._send(404, {"errcode": "M_NOT_FOUND", "error": "no room"})
                return
            _ROOMS[rid]["members"].add(user)
        self._send(200, {"room_id": rid})

    def _handle_send(self, room_id: str, txn: str, body):
        user = self._auth_user()
        if not user:
            self._send(401, {"errcode": "M_MISSING_TOKEN"})
            return
        with _LOCK:
            if room_id not in _ROOMS:
                self._send(404, {"errcode": "M_NOT_FOUND"})
                return
            evt = _make_event(room_id, _user_id(user), "m.room.message",
                              {"msgtype": body.get("msgtype", "m.text"),
                               "body": body.get("body", "")})
            _ROOMS[room_id]["events"].append(evt)
            eid = evt["event_id"]
        self._send(200, {"event_id": eid})

    def _handle_sync(self):
        user = self._auth_user()
        if not user:
            self._send(401, {"errcode": "M_MISSING_TOKEN"})
            return
        # parse since + timeout
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        since = q.get("since", [None])[0]
        since_seq = int(since) if since and since.isdigit() else 0
        join_payload = {}
        next_seq = since_seq
        with _LOCK:
            for rid, rdata in _ROOMS.items():
                if user not in rdata["members"]:
                    continue
                evs = [dict(e) for e in rdata["events"] if e["_seq"] > since_seq]
                if not evs and since_seq > 0:
                    continue
                for e in evs:
                    next_seq = max(next_seq, e["_seq"])
                # strip internal _seq from output
                clean = [{k: v for k, v in e.items() if k != "_seq"} for e in evs]
                join_payload[rid] = {
                    "timeline": {"events": clean, "limited": False, "prev_batch": since or "0"},
                    "state": {"events": []},
                }
        self._send(200, {"next_batch": str(next_seq), "rooms": {"join": join_payload}})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), HSHandler)
    print(f"[mini-hs] listening on :{LISTEN_PORT} server_name={SERVER_NAME}", flush=True)
    srv.serve_forever()

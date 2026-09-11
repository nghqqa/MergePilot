"""Phase 14.2H-WD-COPAW-HIGH-RISK-FIX-AUDIT — mandated regression tests.

Covers the eight audit items against the two fixes:
  Fix A (task storage): project-scoped ``shared/projects/<pid>/tasks/<tid>/``
  layout with guarded legacy fallback, and the hardened delegate reuse branch.
  Fix B (matrix channel): sync-token replay window + seen-event ledger +
  robust DM classification.

Run under pytest (pytest-asyncio) or standalone: ``python tests/test_fix_audit.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

try:
    import pytest
except ImportError:  # standalone runner without pytest installed
    class _Mark:
        @staticmethod
        def asyncio(fn):
            return fn

    class _PytestShim:
        mark = _Mark()
        setattr_fixture = None

    pytest = _PytestShim()

import copaw_worker.matrix_channel as mc
import copaw_worker.hooks.tools.taskflow as taskflow_tool
from copaw_worker.hooks.tools.projectflow import projectflow
from copaw_worker.hooks.tools.taskflow import taskflow
from copaw_worker.matrix_channel import MatrixChannel
from copaw_worker.task import FileSystemTaskStore, TaskflowError

HS = "p14h2-wd-matrix:6167"
MANAGER = f"@p14h2-copaw-worker-manager:{HS}"
REVIEWER = f"@p14h2-copaw-worker-reviewer:{HS}"
ADMIN = f"@admin:{HS}"
TEAM_ROOM = "!8JQYuign7W0GMBDBDU:hs"


# ---------------------------------------------------------------- helpers

def _make_store(tmp_path: Path, name: str = "w") -> tuple[FileSystemTaskStore, Path]:
    workspace = tmp_path / name / ".copaw" / "workspaces" / "default"
    store = FileSystemTaskStore(workspace)
    return store, workspace


def _write_flat_meta(
    workspace: Path,
    task_id: str,
    project_id: str,
    status: str = "assigned",
    event_id: str | None = "$stale-flat-event",
) -> Path:
    """Craft a legacy flat-layout task meta (pre-fix leftover)."""
    d = workspace / "shared" / "tasks" / task_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "task_id": task_id,
        "project_id": project_id,
        "task_title": f"{project_id} {task_id}",
        "assigned_to": REVIEWER,
        "room_id": f"room:{TEAM_ROOM}",
        "status": status,
        "depends_on": [],
        "assigned_at": "2026-08-29T03:59:00Z",
    }
    if event_id is not None:
        meta["event_id"] = event_id
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


async def _create_project_with_review(project_id: str, title: str) -> None:
    resp = _json(
        await projectflow(
            action="create_project",
            payload={
                "projectId": project_id,
                "title": title,
                "source": "team-admin",
                "requester": ADMIN,
            },
        ),
    )
    assert resp["ok"] is True
    resp = _json(
        await projectflow(
            action="plan_dag",
            payload={
                "projectId": project_id,
                "tasks": [
                    {
                        "taskId": "review-1",
                        "title": f"Review for {project_id}",
                        "assignedTo": REVIEWER,
                        "dependsOn": [],
                    },
                ],
            },
        ),
    )
    assert resp["ok"] is True


def _json(response):
    item = response.content[0]
    text = item.get("text") if isinstance(item, dict) else item.text
    return json.loads(text)


def _setup_tool_env(tmp_path: Path, monkeypatch, actor: str = MANAGER):
    working_dir = tmp_path / "worker" / ".copaw"
    workspace = working_dir / "workspaces" / "default"
    # Set BOTH env vars: copaw.app._app writes QWENPAW_WORKING_DIR into
    # os.environ when instantiated, and _working_dir() prefers it.
    monkeypatch.setenv("COPAW_WORKING_DIR", str(working_dir))
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(working_dir))
    monkeypatch.setenv("AGENTTEAMS_MATRIX_USER_ID", actor)
    runtime_dir = working_dir.parent / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "runtime.yaml").write_text(
        "kind: MemberRuntimeConfig\n"
        "member:\n"
        "  role: team_leader\n"
        "team:\n"
        "  teamRoomId: \"!8JQYuign7W0GMBDBDU:hs\"\n"
        "  leaderDmRoomId: \"!leader-dm:hs\"\n",
        encoding="utf-8",
    )
    sync_mock = MagicMock()
    monkeypatch.setattr(taskflow_tool, "create_sync", lambda: sync_mock)
    return workspace, sync_mock


def _install_notify_recorder(monkeypatch, events: list):
    async def fake_notify(**kwargs):
        events.append(kwargs)
        return {
            "sent": True,
            "eventId": f"$evt{len(events)}",
            "roomId": kwargs.get("room_id", ""),
            "assignee": REVIEWER,
        }

    monkeypatch.setattr(taskflow_tool, "_notify_task_assignment", fake_notify)


# ------------------------------------------------- T1: project isolation

def test_t1_project_scoped_storage_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "w" / ".copaw"))
    store, workspace = _make_store(tmp_path, "w")

    from copaw_worker.task import TaskMeta

    meta_a = TaskMeta(
        task_id="review-1", project_id="proj-a", task_title="A review",
        assigned_to=REVIEWER, status="assigned", event_id="$evt-a",
    )
    meta_b = TaskMeta(
        task_id="review-1", project_id="proj-b", task_title="B review",
        assigned_to=REVIEWER, status="assigned", event_id="$evt-b",
    )
    store.write_task_meta(meta_a)
    store.write_task_meta(meta_b)

    dir_a = workspace / "shared" / "projects" / "proj-a" / "tasks" / "review-1"
    dir_b = workspace / "shared" / "projects" / "proj-b" / "tasks" / "review-1"
    assert (dir_a / "meta.json").exists() and (dir_b / "meta.json").exists()

    assert store.read_task_meta("review-1", project_id="proj-a").event_id == "$evt-a"
    assert store.read_task_meta("review-1", project_id="proj-b").event_id == "$evt-b"

    # No explicit project + two owning projects => ambiguous, never silent.
    try:
        store.read_task_meta("review-1")
        raised = False
    except TaskflowError as exc:
        raised = "multiple projects" in str(exc)
    assert raised, "unscoped read of a shared task_id must be ambiguous"

    # A foreign project cannot read the other project's task.
    try:
        store.read_task_meta("review-1", project_id="proj-c")
        raised = False
    except TaskflowError:
        raised = True
    assert raised


def test_t1b_legacy_flat_visible_without_project_only(tmp_path, monkeypatch):
    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "w" / ".copaw"))
    store, workspace = _make_store(tmp_path, "w")
    _write_flat_meta(workspace, "fix-1", "copaw-sandbox", status="submitted")

    meta = store.read_task_meta("fix-1")
    assert meta.project_id == "copaw-sandbox"
    # Explicit project guard: foreign flat leftover is invisible to proj-x.
    try:
        store.read_task_meta("fix-1", project_id="proj-x")
        raised = False
    except TaskflowError:
        raised = True
    assert raised


# ------------------------- T2 + T3: reuse branch & same-task_id regression

@pytest.mark.asyncio
async def test_t2_t3_cross_project_same_task_id_delegates_freshly(
    tmp_path, monkeypatch,
):
    workspace, sync_mock = _setup_tool_env(tmp_path, monkeypatch)
    events: list = []
    _install_notify_recorder(monkeypatch, events)

    await _create_project_with_review("copaw-sandbox", "Sandbox")
    resp = _json(
        await taskflow(
            action="delegate_task",
            payload={
                "projectId": "copaw-sandbox",
                "taskId": "review-1",
                "roomId": f"room:{TEAM_ROOM}",
                "spec": "sandbox spec; write shared/tasks/review-1/result.md",
            },
        ),
    )
    assert resp["ok"] is True and resp["notification"]["eventId"] == "$evt1"
    # New layout: task stored project-scoped.
    new_dir = workspace / "shared" / "projects" / "copaw-sandbox" / "tasks" / "review-1"
    assert (new_dir / "meta.json").exists()
    assert not (workspace / "shared" / "tasks" / "review-1" / "meta.json").exists()

    # Simulate the audited bug leftover: another project's assigned task
    # with a stale event in the LEGACY flat namespace.
    _write_flat_meta(workspace, "review-1", "copaw-OTHER", status="assigned",
                     event_id="$stale-flat-event")

    await _create_project_with_review("copaw-high-risk-human-gate", "High risk")
    resp = _json(
        await taskflow(
            action="delegate_task",
            payload={
                "projectId": "copaw-high-risk-human-gate",
                "taskId": "review-1",
                "roomId": f"room:{TEAM_ROOM}",
                "spec": "high risk spec; write shared/tasks/review-1/result.md",
            },
        ),
    )
    # Must NOT reuse the foreign/stale assignment: a fresh event is produced.
    assert resp["ok"] is True
    assert not resp["notification"].get("reused")
    assert resp["notification"]["eventId"] == "$evt2"
    assert len(events) == 2

    hr_dir = workspace / "shared" / "projects" / "copaw-high-risk-human-gate" / "tasks" / "review-1"
    meta = json.loads((hr_dir / "meta.json").read_text())
    assert meta["event_id"] == "$evt2"
    assert meta["project_id"] == "copaw-high-risk-human-gate"


@pytest.mark.asyncio
async def test_t2b_reuse_branch_semantics(tmp_path, monkeypatch):
    workspace, sync_mock = _setup_tool_env(tmp_path, monkeypatch)
    events: list = []
    _install_notify_recorder(monkeypatch, events)
    await _create_project_with_review("proj-r", "Reuse project")

    payload = {
        "projectId": "proj-r",
        "taskId": "review-1",
        "roomId": f"room:{TEAM_ROOM}",
        "spec": "spec; result at shared/tasks/review-1/result.md",
    }
    resp = _json(await taskflow(action="delegate_task", payload=payload))
    assert resp["notification"]["eventId"] == "$evt1"

    # Same project, assigned WITH event_id -> reuse, no new notification.
    resp = _json(await taskflow(action="delegate_task", payload=payload))
    assert resp["notification"]["reused"] is True
    assert resp["notification"]["eventId"] == "$evt1"
    assert len(events) == 1

    # Same project, assigned but event_id lost -> must resend (no reuse).
    meta_path = (
        workspace / "shared" / "projects" / "proj-r" / "tasks" / "review-1" / "meta.json"
    )
    meta = json.loads(meta_path.read_text())
    meta["event_id"] = ""
    meta_path.write_text(json.dumps(meta))
    resp = _json(await taskflow(action="delegate_task", payload=payload))
    assert resp["ok"] is True
    assert not resp["notification"].get("reused")
    assert len(events) == 2


@pytest.mark.asyncio
async def test_t2c_legacy_same_project_stale_meta_not_trusted_for_reuse(
    tmp_path, monkeypatch,
):
    """The audited bug: a flat-namespace 'assigned' meta with a stale
    event_id (written into shared/tasks/<tid> by the pre-fix code) must not
    satisfy the reuse check even when its project_id matches."""
    workspace, sync_mock = _setup_tool_env(tmp_path, monkeypatch)
    events: list = []
    _install_notify_recorder(monkeypatch, events)

    await _create_project_with_review("copaw-high-risk-human-gate", "High risk")
    # Simulate the audited contamination: THIS project's delegation recorded
    # in the LEGACY flat layout with a stale event id, no project-scoped dir.
    _write_flat_meta(
        workspace, "review-1", "copaw-high-risk-human-gate",
        status="assigned", event_id="$stale-sandbox-event",
    )

    resp = _json(
        await taskflow(
            action="delegate_task",
            payload={
                "projectId": "copaw-high-risk-human-gate",
                "taskId": "review-1",
                "roomId": f"room:{TEAM_ROOM}",
                "spec": "high risk spec; result at shared/tasks/review-1/result.md",
            },
        ),
    )
    assert resp["ok"] is True
    assert not resp["notification"].get("reused")
    assert resp["notification"]["eventId"] == "$evt1"
    assert len(events) == 1
    meta = json.loads(
        (workspace / "shared" / "projects" / "copaw-high-risk-human-gate" / "tasks" / "review-1" / "meta.json")
        .read_text(),
    )
    assert meta["event_id"] == "$evt1"


@pytest.mark.asyncio
async def test_t3b_ack_submit_use_project_scoped_paths(tmp_path, monkeypatch):
    workspace, sync_mock = _setup_tool_env(
        tmp_path, monkeypatch, actor=REVIEWER,
    )
    # Leader role config is required only for delegate; ack/submit run as worker.
    events: list = []
    _install_notify_recorder(monkeypatch, events)
    await _create_project_with_review("proj-flow", "Flow")
    resp = _json(
        await taskflow(
            action="delegate_task",
            payload={
                "projectId": "proj-flow",
                "taskId": "review-1",
                "roomId": f"room:{TEAM_ROOM}",
                "spec": "spec; deliver shared/tasks/review-1/result.md",
            },
        ),
    )
    assert resp["ok"] is True

    # Worker acks with only taskId (no projectId): path is resolved via meta.
    monkeypatch.setenv("AGENTTEAMS_MATRIX_USER_ID", REVIEWER)
    resp = _json(await taskflow(action="ack_task", payload={"taskId": "review-1"}))
    assert resp["ok"] is True
    assert resp["task"]["status"] == "in_progress"
    pushed = [
        c.args[0] for c in sync_mock.push_shared_path.call_args_list
    ]
    assert "shared/projects/proj-flow/tasks/review-1/" in pushed

    result_file = (
        workspace / "shared" / "projects" / "proj-flow" / "tasks" / "review-1" / "result.md"
    )
    result_file.write_text(
        "STATUS: SUCCESS\n"
        "SUMMARY: done\n\n"
        "DELIVERABLES:\n"
        "- shared/projects/proj-flow/tasks/review-1/findings.md\n",
        encoding="utf-8",
    )
    resp = _json(
        await taskflow(action="submit_task", payload={"taskId": "review-1"}),
    )
    assert resp["ok"] is True and resp["task"]["status"] == "submitted"


# ------------------------------------------ Fix B: matrix channel behavior

def _make_channel(tmp_path, monkeypatch, *, replay_window=200):
    working_dir = tmp_path / "w" / ".copaw"
    monkeypatch.setenv("COPAW_WORKING_DIR", str(working_dir))
    monkeypatch.setenv("QWENPAW_WORKING_DIR", str(working_dir))
    monkeypatch.setattr(mc, "REPLAY_WINDOW_EVENTS", replay_window)
    ch = MatrixChannel.__new__(MatrixChannel)
    ch._user_id = REVIEWER
    ch._cfg = SimpleNamespace(
        dm_policy="allowlist",
        allow_from={mc._normalize_user_id(MANAGER), mc._normalize_user_id(ADMIN)},
        group_policy="allowlist",
        group_allow_from={mc._normalize_user_id(MANAGER), mc._normalize_user_id(ADMIN)},
        groups={},
        history_limit=20,
    )
    ch._client = _FakeMatrixClient()
    ch._seen_event_ids = set()
    ch._room_histories = {}
    ch._sync_token_path  # ensure method exists
    captured: list = []

    def _enqueue(payload):
        captured.append(payload)

    ch._enqueue = _enqueue
    ch.enqueued = captured

    async def _noop(*a, **k):
        return None

    ch._send_read_receipt = _noop
    ch._send_typing = _noop
    ch._apply_history_to_parts = lambda room_id, parts: parts
    ch._clear_history = lambda room_id: None
    ch._get_display_name = lambda room, uid: None
    return ch


class _FakeMatrixClient:
    def __init__(self, joined_members_count=None, fail_joined_members=False):
        self._count = joined_members_count
        self._fail = fail_joined_members

    async def joined_members(self, room_id):
        if self._fail:
            raise RuntimeError("homeserver unreachable")
        members = [
            SimpleNamespace(user_id=f"@u{i}:hs") for i in range(self._count or 0)
        ]
        return SimpleNamespace(members=members)


def _event(event_id, body, *, mentions=None, formatted=None, sender=MANAGER):
    content = {"body": body}
    if mentions:
        content["m.mentions"] = {"user_ids": mentions}
    if formatted:
        content["formatted_body"] = formatted
    return SimpleNamespace(
        sender=sender,
        body=body,
        event_id=event_id,
        source={"content": content},
        server_timestamp=0,
    )


def _room(users):
    return SimpleNamespace(room_id=TEAM_ROOM, users=users)


def test_t6_sync_token_replay_window(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "REPLAY_WINDOW_EVENTS", 200)
    ch = _make_channel(tmp_path, monkeypatch)
    token_path = ch._sync_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("25396")
    assert ch._load_sync_token() == "25196"

    # Token below the window floor -> clamp to 0.
    token_path.write_text("5")
    assert ch._load_sync_token() == "0"

    # Window disabled -> legacy passthrough.
    monkeypatch.setattr(mc, "REPLAY_WINDOW_EVENTS", 0)
    token_path.write_text("25396")
    assert ch._load_sync_token() == "25396"

    # Non-numeric token (opaque synapse style) -> unchanged even with window.
    monkeypatch.setattr(mc, "REPLAY_WINDOW_EVENTS", 200)
    token_path.write_text("s25396_1")
    assert ch._load_sync_token() == "s25396_1"


def test_t4_t7_token_advance_replay_and_idempotency(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, replay_window=200)
    event = _event("$delegation-1", f"{REVIEWER} You are assigned task **review-1**")
    room = _room({f"@u{i}:hs" for i in range(5)})  # group room, mention present

    # 1st delivery: enqueued and recorded.
    asyncio.run(ch._on_room_event(room, event))
    assert len(ch.enqueued) == 1
    assert ch._seen_events_path().exists()

    # Duplicate delivery within the same process: suppressed.
    asyncio.run(ch._on_room_event(room, event))
    assert len(ch.enqueued) == 1

    # Simulate restart: fresh channel state restores the ledger from disk.
    ch2 = _make_channel(tmp_path, monkeypatch, replay_window=200)
    ch2._seen_event_ids = ch2._load_seen_events()
    asyncio.run(ch2._on_room_event(room, event))
    assert len(ch2.enqueued) == 0, "ledger must survive restart (exactly-once)"

    # A different event (new delegation) is still delivered after restart.
    event2 = _event("$delegation-2", f"{REVIEWER} You are assigned task **review-1**")
    asyncio.run(ch2._on_room_event(room, event2))
    assert len(ch2.enqueued) == 1
    assert ch2.enqueued[0]["meta"]["event_id"] == "$delegation-2"


def test_t4b_enqueue_failure_not_recorded(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, replay_window=200)
    event = _event("$delegation-3", f"{REVIEWER} You are assigned task **review-1**")
    room = _room({f"@u{i}:hs" for i in range(5)})

    def _boom(payload):
        raise RuntimeError("queue down")

    ch._enqueue = _boom
    try:
        asyncio.run(ch._on_room_event(room, event))
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert "$delegation-3" not in ch._load_seen_events(), (
        "failed enqueue must not advance the effective position"
    )

    # Queue recovers -> replay delivers exactly once.
    ch._enqueue = lambda payload: ch.enqueued.append(payload)
    asyncio.run(ch._on_room_event(room, event))
    assert len(ch.enqueued) == 1
    assert "$delegation-3" in ch._load_seen_events()


def test_t5_mention_mxid_filtering(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, replay_window=200)
    group = _room({f"@u{i}:hs" for i in range(5)})

    # Group room without any mention -> recorded to history, not enqueued.
    plain = _event("$m1", "Please review PR#2", sender=MANAGER)
    asyncio.run(ch._on_room_event(group, plain))
    assert len(ch.enqueued) == 0
    assert TEAM_ROOM in ch._room_histories and ch._room_histories[TEAM_ROOM]

    # Layer 3: full MXID in plain text body.
    m2 = _event("$m2", f"{REVIEWER} You are assigned task **review-1**")
    asyncio.run(ch._on_room_event(group, m2))
    assert len(ch.enqueued) == 1

    # Layer 1: structured m.mentions user_ids.
    m3 = _event("$m3", "You are assigned task **review-1**", mentions=[REVIEWER])
    asyncio.run(ch._on_room_event(group, m3))
    assert len(ch.enqueued) == 2

    # Layer 2: matrix.to link in formatted_body.
    link = f"https://matrix.to/#/{REVIEWER}"
    m4 = _event("$m4", "review please", formatted=f'<a href="{link}">reviewer</a>')
    asyncio.run(ch._on_room_event(group, m4))
    assert len(ch.enqueued) == 3


def test_t5b_dm_without_mention_is_delivered(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, replay_window=200)
    dm = SimpleNamespace(room_id="!dm:hs", users={MANAGER: None, REVIEWER: None})
    event = _event("$dm1", "redelivery without mention", sender=ADMIN)
    asyncio.run(ch._on_room_event(dm, event))
    assert len(ch.enqueued) == 1
    assert ch.enqueued[0]["meta"]["is_dm"] is True

    # Non-allowlisted DM sender is still blocked.
    event2 = _event("$dm2", "stranger", sender="@intruder:hs")
    asyncio.run(ch._on_room_event(dm, event2))
    assert len(ch.enqueued) == 1


def test_t8_dm_classification_with_empty_room_state(tmp_path, monkeypatch):
    # room.users empty (replay/catch-up): joined_members says 2 -> DM.
    ch = _make_channel(tmp_path, monkeypatch, replay_window=200)
    ch._client = _FakeMatrixClient(joined_members_count=2)
    empty = SimpleNamespace(room_id="!dm:hs", users={})
    event = _event("$dm3", "operator redelivery", sender=ADMIN)
    asyncio.run(ch._on_room_event(empty, event))
    assert len(ch.enqueued) == 1

    # joined_members says 3 -> group room, no mention -> dropped to history.
    ch2 = _make_channel(tmp_path, monkeypatch, replay_window=200)
    ch2._client = _FakeMatrixClient(joined_members_count=3)
    asyncio.run(ch2._on_room_event(empty, event))
    assert len(ch2.enqueued) == 0

    # joined_members fails -> fail safe to group semantics, no crash.
    ch3 = _make_channel(tmp_path, monkeypatch, replay_window=200)
    ch3._client = _FakeMatrixClient(fail_joined_members=True)
    asyncio.run(ch3._on_room_event(empty, event))
    assert len(ch3.enqueued) == 0


# ---------------------------------------------------- standalone runner

def _monkeypatch_shim():
    class MonkeyPatch:
        def __init__(self):
            self._env = {}
            self._attrs = []

        def setenv(self, name, value):
            self._env[name] = os.environ.get(name)
            os.environ[name] = str(value)

        def setattr(self, obj, name, value):
            self._attrs.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for name, value in self._env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            for obj, name, value in self._attrs:
                setattr(obj, name, value)

    return MonkeyPatch()


def _run_standalone() -> int:
    import inspect
    import os
    import tempfile

    import sys

    this = sys.modules[__name__]
    failures = 0
    for name, fn in sorted(vars(this).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        kwargs = {}
        tmp_ctx = tempfile.TemporaryDirectory() if "tmp_path" in inspect.signature(fn).parameters else None
        try:
            if tmp_ctx:
                kwargs["tmp_path"] = Path(tmp_ctx.__enter__())
            if "monkeypatch" in inspect.signature(fn).parameters:
                kwargs["monkeypatch"] = _monkeypatch_shim()
            if inspect.iscoroutinefunction(fn):
                asyncio.run(fn(**kwargs))
            else:
                fn(**kwargs)
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc}")
            failures += 1
        finally:
            if kwargs.get("monkeypatch"):
                kwargs["monkeypatch"].undo()
            if tmp_ctx:
                tmp_ctx.__exit__(None, None, None)
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())

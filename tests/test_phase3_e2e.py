"""Phase 3 deliverable, end-to-end through the HTTP API: 'remind me at 6pm'
creates a real reminder; due reminders fire once; destructive confirmation
round-trips through chat."""

import json
import time

from fastapi.testclient import TestClient

from jarvis.core import db
from jarvis.hub.app import create_app
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.tools.tasks import pop_due_reminders
from tests.conftest import TEST_API_KEY
from tests.test_toolloop import ScriptedRouter


def make_client(cfg, responses):
    app = create_app(cfg, embedder=FakeEmbedder(), router=ScriptedRouter(responses))
    return TestClient(app, headers={"x-api-key": TEST_API_KEY})


def test_remind_me_creates_task_via_chat(cfg):
    responses = [
        json.dumps({"action": "tool", "tool": "task_add",
                    "args": {"title": "call the bank", "due": "18:00"}}),
        json.dumps({"action": "final",
                    "reply": "Done — I'll remind you at 18:00 to call the bank."}),
    ]
    with make_client(cfg, responses) as client:
        r = client.post("/chat", json={"text": "remind me at 6pm to call the bank"})
        assert r.status_code == 200
        assert r.json()["tools_used"] == ["task_add"]

    conn = db.connect(cfg.db_path)
    task = conn.execute("SELECT * FROM tasks").fetchone()
    conn.close()
    assert task["title"] == "call the bank"
    assert task["remind"] == 1
    assert task["due_ts"] > time.time()


def test_due_reminders_fire_exactly_once(cfg):
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO tasks (title, due_ts, remind, created_ts)"
        " VALUES ('water the plants', ?, 1, ?)",
        (time.time() - 5, time.time()),
    )
    conn.execute(  # not yet due — must NOT fire
        "INSERT INTO tasks (title, due_ts, remind, created_ts)"
        " VALUES ('future thing', ?, 1, ?)",
        (time.time() + 3600, time.time()),
    )
    conn.close()

    fired = pop_due_reminders(cfg.db_path)
    assert [f["title"] for f in fired] == ["water the plants"]
    assert pop_due_reminders(cfg.db_path) == []  # second sweep: nothing


def test_confirmation_roundtrip_via_chat(cfg):
    responses = [
        json.dumps({"action": "tool", "tool": "homelab_reboot",
                    "args": {"node": "mini"}}),
    ]
    with make_client(cfg, responses) as client:
        r = client.post("/chat", json={"text": "reboot the mini"})
        reply = r.json()["reply"]
        assert "confirm " in reply
        token = reply.split("confirm ")[1].split("`")[0].strip()

        # deterministic interception — no LLM involved in confirmation
        r = client.post("/chat", json={"text": f"confirm {token}"})
        assert r.json()["tier"] == "tool"
        # no 'mini' node configured in tests → honest tool error surfaced
        assert "unknown node" in r.json()["reply"]

    # audit shows the park and the confirm
    conn = db.connect(cfg.db_path)
    actions = [row["action"] for row in
               conn.execute("SELECT action FROM audit_log")]
    conn.close()
    assert "tool.homelab_reboot" in actions
    assert "tool.confirm" in actions


def test_bogus_confirm_token_is_refused(cfg):
    with make_client(cfg, [json.dumps({"action": "final", "reply": "x"})]) as client:
        r = client.post("/chat", json={"text": "confirm abc123"})
        assert "no pending action" in r.json()["reply"]

import time
from datetime import datetime

import pytest

from jarvis.core.audit import AuditLog
from jarvis.core.bus import Bus, Message
from jarvis.core.crypto import Vault
from jarvis.core.killswitch import KillSwitch
from jarvis.proactive.engine import ProactiveEngine
from jarvis.proactive.followups import FollowupStore, detect_commitment
from jarvis.proactive.schedule import initial_next_run, next_run_after
from jarvis.router.router import RouteResult
from tests.conftest import TEST_MASTER_KEY


# -- schedule math --------------------------------------------------------------

def test_daily_schedule_next_run():
    base = datetime(2026, 7, 7, 9, 0).timestamp()  # 09:00
    nxt = datetime.fromtimestamp(next_run_after("daily@07:30", base))
    assert (nxt.hour, nxt.minute) == (7, 30)
    assert nxt.day == 8  # 07:30 already passed today → tomorrow

    base2 = datetime(2026, 7, 7, 6, 0).timestamp()  # 06:00
    nxt2 = datetime.fromtimestamp(next_run_after("daily@07:30", base2))
    assert nxt2.day == 7  # later today


def test_everyN_schedule():
    assert next_run_after("everyN:3600", 1000) == 4600
    assert initial_next_run("everyN:3600", 5000) == 5000  # runs promptly first


# -- commitment detection -------------------------------------------------------

def test_detect_commitment_patterns():
    assert detect_commitment("I'll email Sam about the invoice")[0] == \
        "email Sam about the invoice"
    assert detect_commitment("I need to renew the domain tomorrow")[0] == \
        "renew the domain tomorrow"
    assert detect_commitment("remind me to water the plants")[0] == "water the plants"
    assert detect_commitment("what's the weather like?") is None
    assert detect_commitment("I'll go") is None  # too short


def test_detect_commitment_deadline_hint():
    _, by_ts = detect_commitment("I'll call the bank tomorrow")
    assert by_ts is not None and by_ts > time.time()
    _, no_by = detect_commitment("I'll refactor the router")
    assert no_by is None


def test_followup_store_roundtrip(cfg):
    store = FollowupStore(cfg.db_path, Vault(TEST_MASTER_KEY))
    fid = store.maybe_record("I'll email Sam tomorrow")
    assert fid
    open_fu = store.open_followups()
    assert open_fu[0]["text"] == "email Sam tomorrow"
    assert store.resolve(fid, "done")
    assert store.open_followups() == []


def test_followup_encrypted_at_rest(cfg):
    store = FollowupStore(cfg.db_path, Vault(TEST_MASTER_KEY))
    store.record("email the SECRET-CLIENT about pricing", None)
    assert b"SECRET-CLIENT" not in cfg.db_path.read_bytes()


# -- engine ---------------------------------------------------------------------

class StubRouter:
    def __init__(self, reply="A calm summary, sir."):
        self.reply = reply

    async def chat(self, task, messages, privacy_tags=()):
        return RouteResult(self.reply, "stub", "m", 1.0)


class StubTelegram:
    def __init__(self):
        self.sent: list[str] = []

    async def send_to_operator(self, text):
        self.sent.append(text)
        return True


def make_engine(cfg, router=None, telegram=None, store=None):
    return ProactiveEngine(
        cfg, store=store, router=router or StubRouter(), bus=Bus(cfg.db_path),
        audit=AuditLog(cfg.db_path), killswitch=KillSwitch(cfg.db_path),
        vault=Vault(TEST_MASTER_KEY), telegram=telegram or StubTelegram(),
    )


def test_ensure_default_jobs_idempotent(cfg):
    from jarvis.core import db

    engine = make_engine(cfg)
    engine.ensure_default_jobs()
    engine.ensure_default_jobs()
    conn = db.connect(cfg.db_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM scheduled_jobs")}
    conn.close()
    assert {"morning_digest", "evening_digest", "nightly_consolidation"} <= names


def test_claim_due_jobs_is_atomic_no_double_fire(cfg):
    from jarvis.core import db

    engine = make_engine(cfg)
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO scheduled_jobs (name, kind, schedule, next_run, created_ts)"
        " VALUES ('t', 'digest', 'everyN:3600', ?, ?)",
        (time.time() - 1, time.time()),
    )
    conn.close()
    first = engine._claim_due_jobs()
    second = engine._claim_due_jobs()  # already advanced past now
    assert len(first) == 1
    assert second == []  # not claimed twice


async def test_morning_digest_pushes(cfg):
    from jarvis.core import db

    conn = db.connect(cfg.db_path)
    conn.execute("INSERT INTO tasks (title, due_ts, remind, created_ts)"
                 " VALUES ('ship phase 4', ?, 0, ?)", (time.time() + 3600, time.time()))
    conn.close()
    tg = StubTelegram()
    engine = make_engine(cfg, telegram=tg)
    await engine._run_digest({"name": "morning_digest"})
    assert tg.sent and "Good morning" in tg.sent[0]


async def test_digest_degrades_to_plain_when_all_tiers_down(cfg):
    from jarvis.router.router import DegradedError

    class DeadRouter:
        async def chat(self, task, messages, privacy_tags=()):
            raise DegradedError([("all", "down")])

    tg = StubTelegram()
    engine = make_engine(cfg, router=DeadRouter(), telegram=tg)
    result = await engine._run_digest({"name": "morning_digest"})
    # still sends something useful (the structured material), doesn't crash
    assert tg.sent
    assert "tasks" in result.lower() or "digest" in result.lower()


async def test_followup_sweep_nudges_overdue(cfg):
    store = FollowupStore(cfg.db_path, Vault(TEST_MASTER_KEY))
    store.record("call the dentist", by_ts=time.time() - 10)  # overdue
    store.record("future thing", by_ts=time.time() + 9999)     # not yet
    tg = StubTelegram()
    engine = make_engine(cfg, telegram=tg)
    out = await engine._run_followup_sweep({"name": "followup_sweep"})
    assert out == "nudged 1"
    assert "call the dentist" in tg.sent[0]


async def test_node_down_anomaly_alert(cfg):
    tg = StubTelegram()
    engine = make_engine(cfg, telegram=tg)
    msg = Message(1, "system.node_status", {"node": "macbook", "from": "up", "to": "down"},
                  "system", time.time())
    await engine.on_node_status(msg)
    assert any("macbook" in s and "down" in s for s in tg.sent)


async def test_anomaly_suppressed_when_paused(cfg):
    tg = StubTelegram()
    engine = make_engine(cfg, telegram=tg)
    KillSwitch(cfg.db_path).pause("test")
    msg = Message(1, "system.node_status", {"node": "hub", "to": "down"}, "system", time.time())
    await engine.on_node_status(msg)
    assert tg.sent == []  # nothing autonomous fires while paused


async def test_nightly_consolidation_writes_summary(cfg):
    from datetime import timedelta

    from jarvis.memory.embeddings import FakeEmbedder
    from jarvis.memory.store import MemoryStore

    store = MemoryStore(cfg, Vault(TEST_MASTER_KEY), FakeEmbedder())
    yesterday_noon = (datetime.now() - timedelta(days=1)).replace(hour=12).timestamp()
    await store.ingest("worked on the router failover logic", kind="chat",
                       source="test", ts=yesterday_noon)

    engine = make_engine(cfg, store=store)
    result = await engine._run_consolidation({"name": "nightly_consolidation"})
    assert result != "nothing to consolidate"

    from jarvis.core import db
    conn = db.connect(cfg.db_path)
    rows = conn.execute("SELECT day, event_count FROM daily_summaries").fetchall()
    conn.close()
    assert rows and rows[0]["event_count"] >= 1

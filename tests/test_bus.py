import asyncio

from jarvis.core.bus import Bus


def make_bus(cfg, **kw) -> Bus:
    return Bus(cfg.db_path, poll_interval_s=0.05, **kw)


def test_publish_fetch_ack(cfg):
    bus = make_bus(cfg)
    m1 = bus.publish("capture.note", {"text": "hello"})
    bus.publish("capture.note", {"text": "world"})

    msgs = bus.fetch("memory", ["capture.note"])
    assert [m.payload["text"] for m in msgs] == ["hello", "world"]

    bus.ack("memory", "capture.note", m1)
    msgs = bus.fetch("memory", ["capture.note"])
    assert [m.payload["text"] for m in msgs] == ["world"]


def test_fanout_independent_cursors(cfg):
    """Two consumers each see every message (pub/sub, not work-stealing)."""
    bus = make_bus(cfg)
    mid = bus.publish("system.node_status", {"node": "macbook", "to": "down"})

    for consumer in ("proactive", "dashboard"):
        msgs = bus.fetch(consumer, ["system.node_status"])
        assert len(msgs) == 1 and msgs[0].id == mid

    bus.ack("proactive", "system.node_status", mid)
    assert bus.fetch("proactive", ["system.node_status"]) == []
    assert len(bus.fetch("dashboard", ["system.node_status"])) == 1


async def test_consumer_loop_processes_and_acks(cfg):
    bus = make_bus(cfg)
    seen: list[str] = []
    stop = asyncio.Event()

    async def handler(msg):
        seen.append(msg.payload["text"])
        if len(seen) == 2:
            stop.set()

    bus.publish("chat.in", {"text": "a"})
    bus.publish("chat.in", {"text": "b"})
    await asyncio.wait_for(bus.run_consumer("cognition", ["chat.in"], handler, stop), timeout=5)
    assert seen == ["a", "b"]
    assert bus.fetch("cognition", ["chat.in"]) == []  # acked


async def test_poison_message_dead_letters_and_unblocks(cfg):
    """A handler that always fails must not wedge the topic forever."""
    bus = make_bus(cfg, max_attempts=3)
    bus.publish("chat.in", {"text": "poison"})
    bus.publish("chat.in", {"text": "good"})
    seen: list[str] = []
    stop = asyncio.Event()

    async def handler(msg):
        if msg.payload["text"] == "poison":
            raise RuntimeError("boom")
        seen.append(msg.payload["text"])
        stop.set()

    await asyncio.wait_for(bus.run_consumer("cognition", ["chat.in"], handler, stop), timeout=10)
    assert seen == ["good"]

    import json

    from jarvis.core import db

    conn = db.connect(cfg.db_path)
    dead = conn.execute("SELECT * FROM bus_dead").fetchall()
    conn.close()
    assert len(dead) == 1
    assert json.loads(dead[0]["payload"])["text"] == "poison"


def test_at_least_once_no_ack_means_redelivery(cfg):
    bus = make_bus(cfg)
    bus.publish("t", {"n": 1})
    first = bus.fetch("c", ["t"])
    second = bus.fetch("c", ["t"])  # no ack in between → same message again
    assert [m.id for m in first] == [m.id for m in second]


def test_prune_removes_old_messages(cfg):
    bus = make_bus(cfg)
    bus.publish("t", {"n": 1})
    assert bus.prune(retention_days=0) == 1
    assert bus.tail() == []

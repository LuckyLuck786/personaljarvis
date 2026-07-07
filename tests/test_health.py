import pytest

from jarvis.core.bus import Bus
from jarvis.core.config import Config, NodeConfig, Secrets
from jarvis.core.db import migrate
from jarvis.hub.health import NodeMonitor
from tests.conftest import TEST_API_KEY, TEST_MASTER_KEY


def test_health_shape(client):
    h = client.get("/health").json()
    assert h["status"] in ("ok", "degraded")
    assert h["killswitch"]["state"] == "active"
    assert h["hub"]["db"] == "ok"
    assert h["hub"]["rss_mb"] > 0
    assert set(h["cloud"]) == {"groq", "cerebras", "gemini"}


@pytest.fixture()
def node_cfg(tmp_path):
    c = Config(
        data_dir=tmp_path,
        nodes={
            "macbook": NodeConfig(
                # unroutable port → guaranteed down, fast
                ollama_url="http://127.0.0.1:1", role="primary_inference",
                check_interval_s=999,
            )
        },
        secrets=Secrets(
            _env_file=None, jarvis_api_key=TEST_API_KEY, jarvis_master_key=TEST_MASTER_KEY
        ),
    )
    migrate(c.db_path)
    return c


async def test_unreachable_macbook_reports_down(node_cfg):
    """The core Phase 0 guarantee: the hub knows when the laptop is asleep."""
    bus = Bus(node_cfg.db_path)
    monitor = NodeMonitor(node_cfg, bus)
    result = await monitor.probe_node("macbook", "http://127.0.0.1:1")
    assert result["status"] == "down"
    assert "error" in result


async def test_down_transition_published_to_bus(node_cfg):
    import asyncio

    bus = Bus(node_cfg.db_path)
    monitor = NodeMonitor(node_cfg, bus)
    # pretend the last probe saw the laptop awake; the real watch loop will
    # find it down and must publish the transition
    monitor.statuses["macbook"] = {"status": "up"}
    task = asyncio.create_task(
        monitor._watch_node("macbook", node_cfg.nodes["macbook"])
    )
    await asyncio.sleep(0.5)
    monitor.stop()
    await asyncio.wait_for(task, timeout=5)

    events = bus.tail("system.node_status")
    assert len(events) == 1
    assert events[0].payload == {"node": "macbook", "from": "up", "to": "down"}

    from jarvis.core import db

    conn = db.connect(node_cfg.db_path)
    rows = conn.execute("SELECT node, status FROM node_status").fetchall()
    conn.close()
    assert rows and rows[-1]["status"] == "down"

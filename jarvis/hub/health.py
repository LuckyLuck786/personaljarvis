"""Node reachability monitoring + hub self-health.

A background loop probes each configured node's Ollama endpoint on its own
interval. Results are cached in memory (so /health never blocks on a probe),
persisted to node_status, and up/down TRANSITIONS are published to the bus
(`system.node_status`) — the Phase 4 proactive engine turns those into
anomaly alerts. This is also what the model router (Phase 1) consults to
decide MacBook vs. cloud.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import psutil

from jarvis.core import db
from jarvis.core.bus import Bus
from jarvis.core.config import Config
from jarvis.core.logging import get_logger

log = get_logger(__name__)

PROBE_TIMEOUT_S = 3.0


class NodeMonitor:
    def __init__(self, cfg: Config, bus: Bus):
        self.cfg = cfg
        self.bus = bus
        self.statuses: dict[str, dict] = {}
        self._stop = asyncio.Event()

    async def probe_node(self, name: str, ollama_url: str) -> dict:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                resp = await client.get(f"{ollama_url.rstrip('/')}/api/tags")
                resp.raise_for_status()
                models = [m.get("name") for m in resp.json().get("models", [])]
            return {
                "status": "up",
                "latency_ms": round((time.monotonic() - start) * 1000, 1),
                "models": models,
                "checked_at": time.time(),
            }
        except Exception as exc:  # noqa: BLE001 — any failure means "down"
            return {
                "status": "down",
                "latency_ms": None,
                "error": type(exc).__name__,
                "checked_at": time.time(),
            }

    def _persist(self, name: str, result: dict) -> None:
        detail = {k: v for k, v in result.items() if k in ("models", "error")}
        conn = db.connect(self.cfg.db_path)
        try:
            conn.execute(
                "INSERT INTO node_status (node, status, latency_ms, detail, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, result["status"], result["latency_ms"], json.dumps(detail),
                 result["checked_at"]),
            )
            # bounded history — disk is cheap but not infinite
            conn.execute(
                "DELETE FROM node_status WHERE node=? AND id NOT IN"
                " (SELECT id FROM node_status WHERE node=? ORDER BY id DESC LIMIT ?)",
                (name, name, self.cfg.health.history_rows_per_node),
            )
        finally:
            conn.close()

    async def _watch_node(self, name: str, node_cfg) -> None:
        while not self._stop.is_set():
            result = await self.probe_node(name, node_cfg.ollama_url)
            previous = self.statuses.get(name, {}).get("status")
            self.statuses[name] = result
            self._persist(name, result)
            if previous is not None and previous != result["status"]:
                log.info("node_transition", node=name, from_=previous, to=result["status"])
                self.bus.publish(
                    "system.node_status",
                    {"node": name, "from": previous, "to": result["status"]},
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=node_cfg.check_interval_s)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._watch_node(name, node_cfg))
            for name, node_cfg in self.cfg.nodes.items()
            if node_cfg.ollama_url
        ]
        if tasks:
            await asyncio.gather(*tasks)

    def stop(self) -> None:
        self._stop.set()


def hub_self_health(cfg: Config, bus: Bus, started_at: float) -> dict:
    proc = psutil.Process()
    disk = psutil.disk_usage(str(cfg.data_dir))
    try:
        conn = db.connect(cfg.db_path)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "db": "ok" if db_ok else "error",
        "bus_backlog": bus.depth(),
        "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "uptime_s": round(time.time() - started_at, 1),
    }


def cloud_tier_status(cfg: Config) -> dict:
    """Presence of API keys only — we don't burn quota validating them here.
    'configured' means a key exists, not that the key is valid."""
    s = cfg.secrets
    return {
        "groq": "configured" if s.groq_api_key else "not_configured",
        "cerebras": "configured" if s.cerebras_api_key else "not_configured",
        "gemini": "configured" if s.gemini_api_key else "not_configured",
    }

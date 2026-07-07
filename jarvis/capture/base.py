"""Collector framework.

A collector is a small async loop that watches one source, normalizes what
it sees into CaptureEvents, redacts secrets AT THE SOURCE (before anything
leaves the process), and hands events to a sink. The sink is either the
hub's authenticated HTTP API (collectors on the MacBook) or an in-process
callable (collectors on the hub / dev collapse) — same collector code
either way.

Collector state (cursors, seen-hashes) persists to a JSON file per
collector so restarts don't re-ingest history.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from jarvis.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class CaptureEvent:
    source: str                 # collector name: 'notes' | 'clipboard' | ...
    kind: str                   # memory kind: 'note' | 'clipboard' | 'file' | 'shell' | 'browser'
    content: str                # normalized, ALREADY-REDACTED text
    meta: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    tags: tuple[str, ...] = ("capture", "personal")


class Redactor:
    def __init__(self, patterns: list[str]):
        self._compiled = [re.compile(p) for p in patterns]

    def redact(self, text: str) -> str:
        for pattern in self._compiled:
            text = pattern.sub("[REDACTED]", text)
        return text


class HttpSink:
    """Forwards events to the hub's /capture/event endpoint (MacBook path)."""

    def __init__(self, hub_url: str, api_key: str):
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key

    async def __call__(self, event: CaptureEvent) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.hub_url}/capture/event",
                    json={**asdict(event), "tags": list(event.tags)},
                    headers={"x-api-key": self.api_key},
                )
                resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            # hub unreachable → drop-with-log for now; collectors keep their
            # cursor un-advanced on failure so events re-emit on next pass
            log.warning("capture_forward_failed", source=event.source,
                        error=type(exc).__name__)
            return False


class CollectorState:
    def __init__(self, state_dir: Path, name: str):
        self.path = state_dir / f"collector_{name}.json"
        try:
            self.data: dict = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Collector:
    """Base class. Subclasses implement poll() → list[CaptureEvent]."""

    name = "base"
    kind = "capture"

    def __init__(self, config: dict, redactor: Redactor, state_dir: Path):
        self.config = config
        self.redactor = redactor
        self.state = CollectorState(state_dir, self.name)
        self.poll_interval_s = float(config.get("poll_interval_s", 30))

    async def poll(self) -> list[CaptureEvent]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def run(self, sink, stop) -> None:
        log.info("collector_started", collector=self.name)
        import asyncio

        while not stop.is_set():
            try:
                events = await self.poll()
                delivered = True
                for event in events:
                    if not await sink(event):
                        delivered = False
                        break
                if delivered:
                    self.state.save()  # advance cursors only on full delivery
            except Exception:
                log.exception("collector_poll_failed", collector=self.name)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass
        log.info("collector_stopped", collector=self.name)

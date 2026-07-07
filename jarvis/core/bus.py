"""SQLite-backed message bus.

Deliberate architecture call: no Redis. The hub has 6 GB RAM and ~400 GB
disk, and this is a single-operator system with modest message rates
(captures, events, commands — not millions/sec). A durable append-only log
in SQLite with per-consumer cursors gives us:

  * fanout pub/sub (each named consumer sees every message on its topics)
  * at-least-once delivery (cursor advances only after the handler returns)
  * retries + dead-lettering per consumer
  * replay/debugging for free (the log IS the history)
  * zero extra resident processes or RAM

Latency is bounded by the poll interval (default 1s) — fine for an
assistant, and CPU cost at 1s polling is negligible.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from jarvis.core import db
from jarvis.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Message:
    id: int
    topic: str
    payload: dict
    actor: str
    ts: float


Handler = Callable[[Message], Awaitable[None]]


class Bus:
    def __init__(self, db_path: str | Path, poll_interval_s: float = 1.0, max_attempts: int = 5):
        self.db_path = db_path
        self.poll_interval_s = poll_interval_s
        self.max_attempts = max_attempts

    # -- publish ------------------------------------------------------------

    def publish(self, topic: str, payload: dict, actor: str = "system") -> int:
        conn = db.connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO bus_messages (topic, payload, actor, ts) VALUES (?, ?, ?, ?)",
                (topic, json.dumps(payload), actor, time.time()),
            )
            return cur.lastrowid
        finally:
            conn.close()

    # -- consume ------------------------------------------------------------

    def fetch(self, consumer: str, topics: list[str], limit: int = 50) -> list[Message]:
        """Messages past this consumer's cursor, oldest first, across topics."""
        conn = db.connect(self.db_path)
        try:
            out: list[Message] = []
            for topic in topics:
                row = conn.execute(
                    "SELECT last_id FROM bus_cursors WHERE consumer=? AND topic=?",
                    (consumer, topic),
                ).fetchone()
                last_id = row["last_id"] if row else 0
                for r in conn.execute(
                    "SELECT id, topic, payload, actor, ts FROM bus_messages"
                    " WHERE topic=? AND id>? ORDER BY id LIMIT ?",
                    (topic, last_id, limit),
                ):
                    out.append(
                        Message(r["id"], r["topic"], json.loads(r["payload"]), r["actor"], r["ts"])
                    )
            out.sort(key=lambda m: m.id)
            return out[:limit]
        finally:
            conn.close()

    def ack(self, consumer: str, topic: str, message_id: int) -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO bus_cursors (consumer, topic, last_id, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(consumer, topic) DO UPDATE SET"
                "  last_id=MAX(last_id, excluded.last_id), updated_at=excluded.updated_at",
                (consumer, topic, message_id, time.time()),
            )
        finally:
            conn.close()

    def _record_failure(self, consumer: str, msg: Message, error: str) -> bool:
        """Track a handler failure. Returns True if the message was dead-lettered
        (caller should then ack past it)."""
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO bus_failures (consumer, message_id, attempts, last_error, updated_at)"
                " VALUES (?, ?, 1, ?, ?)"
                " ON CONFLICT(consumer, message_id) DO UPDATE SET"
                "  attempts=attempts+1, last_error=excluded.last_error,"
                "  updated_at=excluded.updated_at",
                (consumer, msg.id, error, time.time()),
            )
            attempts = conn.execute(
                "SELECT attempts FROM bus_failures WHERE consumer=? AND message_id=?",
                (consumer, msg.id),
            ).fetchone()["attempts"]
            if attempts >= self.max_attempts:
                conn.execute(
                    "INSERT INTO bus_dead (consumer, message_id, topic, payload, last_error, ts)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (consumer, msg.id, msg.topic, json.dumps(msg.payload), error, time.time()),
                )
                log.error("bus_dead_letter", consumer=consumer, message_id=msg.id, topic=msg.topic)
                return True
            return False
        finally:
            conn.close()

    async def run_consumer(
        self,
        consumer: str,
        topics: list[str],
        handler: Handler,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Poll loop: fetch → handle → ack. At-least-once; failed messages are
        retried up to max_attempts, then dead-lettered so one poison message
        can't wedge a topic forever."""
        stop = stop or asyncio.Event()
        log.info("bus_consumer_started", consumer=consumer, topics=topics)
        while not stop.is_set():
            try:
                messages = self.fetch(consumer, topics)
            except Exception:
                log.exception("bus_fetch_failed", consumer=consumer)
                messages = []
            for msg in messages:
                if stop.is_set():
                    break
                try:
                    await handler(msg)
                    self.ack(consumer, msg.topic, msg.id)
                except Exception as exc:  # noqa: BLE001 — bus must survive handler bugs
                    log.exception("bus_handler_failed", consumer=consumer, message_id=msg.id)
                    if self._record_failure(consumer, msg, repr(exc)):
                        self.ack(consumer, msg.topic, msg.id)
                    else:
                        break  # retry this message next poll, keep ordering
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)
            except asyncio.TimeoutError:
                pass
        log.info("bus_consumer_stopped", consumer=consumer)

    # -- maintenance / introspection -----------------------------------------

    def tail(self, topic: str | None = None, after_id: int = 0, limit: int = 50) -> list[Message]:
        conn = db.connect(self.db_path)
        try:
            if topic:
                rows = conn.execute(
                    "SELECT id, topic, payload, actor, ts FROM bus_messages"
                    " WHERE topic=? AND id>? ORDER BY id DESC LIMIT ?",
                    (topic, after_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, topic, payload, actor, ts FROM bus_messages"
                    " WHERE id>? ORDER BY id DESC LIMIT ?",
                    (after_id, limit),
                ).fetchall()
            return [
                Message(r["id"], r["topic"], json.loads(r["payload"]), r["actor"], r["ts"])
                for r in reversed(rows)
            ]
        finally:
            conn.close()

    def depth(self) -> int:
        """Max unconsumed backlog across cursors (0 if no consumers yet)."""
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX((SELECT COALESCE(MAX(id),0) FROM bus_messages m"
                "   WHERE m.topic=c.topic) - c.last_id), 0) AS depth FROM bus_cursors c"
            ).fetchone()
            return max(0, row["depth"] if row and row["depth"] is not None else 0)
        finally:
            conn.close()

    def prune(self, retention_days: int = 14) -> int:
        cutoff = time.time() - retention_days * 86400
        conn = db.connect(self.db_path)
        try:
            cur = conn.execute("DELETE FROM bus_messages WHERE ts < ?", (cutoff,))
            return cur.rowcount
        finally:
            conn.close()

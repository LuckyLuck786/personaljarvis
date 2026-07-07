"""Append-only, hash-chained audit log.

Every action JARVIS takes (control changes, tool executions, model calls)
is recorded. Each row's hash covers the previous row's hash plus the
canonical JSON of the entry, so any tampering breaks verification from
that row onward. `jarvis audit verify` walks the chain.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from jarvis.core import db
from jarvis.core.logging import get_logger

log = get_logger(__name__)

GENESIS = "genesis"


def _entry_hash(prev_hash: str, ts: float, actor: str, action: str, params: str, outcome: str) -> str:
    canonical = json.dumps(
        {"ts": ts, "actor": actor, "action": action, "params": params, "outcome": outcome},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


class AuditLog:
    def __init__(self, db_path: str | Path):
        self.db_path = db_path

    def record(self, actor: str, action: str, params: dict | None = None, outcome: str = "ok") -> int:
        params_json = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
        ts = time.time()
        conn = db.connect(self.db_path)
        try:
            # Single writer per insert: BEGIN IMMEDIATE serializes chain updates.
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prev_hash = row["hash"] if row else GENESIS
                h = _entry_hash(prev_hash, ts, actor, action, params_json, outcome)
                cur = conn.execute(
                    "INSERT INTO audit_log (ts, actor, action, params, outcome, prev_hash, hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, actor, action, params_json, outcome, prev_hash, h),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        log.info("audit", actor=actor, action=action, outcome=outcome)
        return cur.lastrowid

    def tail(self, limit: int = 50) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, ts, actor, action, params, outcome FROM audit_log"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {**dict(r), "params": json.loads(r["params"])} for r in reversed(rows)
            ]
        finally:
            conn.close()

    def verify(self) -> dict:
        """Walk the whole chain. Returns {ok, entries, first_bad_id}."""
        conn = db.connect(self.db_path)
        try:
            prev = GENESIS
            count = 0
            for r in conn.execute(
                "SELECT id, ts, actor, action, params, outcome, prev_hash, hash"
                " FROM audit_log ORDER BY id"
            ):
                expected = _entry_hash(
                    prev, r["ts"], r["actor"], r["action"], r["params"], r["outcome"]
                )
                if r["prev_hash"] != prev or r["hash"] != expected:
                    return {"ok": False, "entries": count, "first_bad_id": r["id"]}
                prev = r["hash"]
                count += 1
            return {"ok": True, "entries": count, "first_bad_id": None}
        finally:
            conn.close()

"""Global kill switch.

One flag, persisted in SQLite so it survives restarts. When paused, the hub
refuses all non-control endpoints, and every autonomous component (proactive
engine, capture ingestion, tool execution) must check `is_paused()` before
acting. Pausing is always available: control endpoints stay up.
"""

from __future__ import annotations

import time
from pathlib import Path

from jarvis.core import db

KEY = "killswitch"
ACTIVE = "active"
PAUSED = "paused"


class KillSwitch:
    def __init__(self, db_path: str | Path):
        self.db_path = db_path

    def _set(self, value: str, reason: str = "") -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (KEY, f"{value}|{reason}", time.time()),
            )
        finally:
            conn.close()

    def state(self) -> dict:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT value, updated_at FROM system_state WHERE key=?", (KEY,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"state": ACTIVE, "reason": "", "since": None}
        value, _, reason = row["value"].partition("|")
        return {"state": value, "reason": reason, "since": row["updated_at"]}

    def is_paused(self) -> bool:
        return self.state()["state"] == PAUSED

    def pause(self, reason: str = "operator request") -> None:
        self._set(PAUSED, reason)

    def resume(self) -> None:
        self._set(ACTIVE, "")

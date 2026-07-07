"""Commitment detection: spot "I'll / I need to / remind me to …" in what
the operator says and record it as a follow-up. Deterministic pattern match
(no LLM) so it's cheap, runs on every message, and never hallucinates a
commitment that wasn't made.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from jarvis.core import db
from jarvis.core.crypto import Vault

PURPOSE = "memory"

# first-person commitment cues; captures the predicate after the cue
_PATTERNS = [
    re.compile(r"\bi'?ll\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+need\s+to\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+have\s+to\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi\s+should\s+(.+)", re.IGNORECASE),
    re.compile(r"\bi'?m\s+going\s+to\s+(.+)", re.IGNORECASE),
    re.compile(r"\bremind\s+me\s+to\s+(.+)", re.IGNORECASE),
]
# crude soft-deadline hints → offset seconds
_DEADLINE_HINTS = {
    "tomorrow": 86400, "tonight": 6 * 3600, "today": 6 * 3600,
    "this week": 5 * 86400, "next week": 10 * 86400,
}
MAX_LEN = 300


def detect_commitment(text: str) -> tuple[str, float | None] | None:
    """→ (commitment_text, by_ts|None) or None. Only the FIRST cue matches,
    to avoid double-recording one sentence."""
    for pat in _PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        predicate = m.group(1).strip().rstrip(".!?")
        if len(predicate) < 3 or len(predicate) > MAX_LEN:
            return None
        lowered = text.lower()
        by_ts = None
        for hint, offset in _DEADLINE_HINTS.items():
            if hint in lowered:
                by_ts = time.time() + offset
                break
        return predicate, by_ts
    return None


class FollowupStore:
    def __init__(self, db_path: str | Path, vault: Vault):
        self.db_path = db_path
        self.vault = vault

    def record(self, text: str, by_ts: float | None) -> int:
        conn = db.connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO followups (text, source_ts, by_ts, created_ts)"
                " VALUES (?, ?, ?, ?)",
                (self.vault.encrypt(PURPOSE, text), time.time(), by_ts, time.time()),
            )
            return cur.lastrowid
        finally:
            conn.close()

    def maybe_record(self, text: str) -> int | None:
        found = detect_commitment(text)
        if found is None:
            return None
        return self.record(found[0], found[1])

    def open_followups(self) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, text, by_ts, status FROM followups"
                " WHERE status IN ('open','nudged') ORDER BY created_ts DESC LIMIT 50"
            ).fetchall()
        finally:
            conn.close()
        return [{"id": r["id"], "by_ts": r["by_ts"], "status": r["status"],
                 "text": self.vault.decrypt_text(PURPOSE, r["text"])} for r in rows]

    def resolve(self, followup_id: int, status: str = "done") -> bool:
        conn = db.connect(self.db_path)
        try:
            cur = conn.execute("UPDATE followups SET status=? WHERE id=?",
                               (status, followup_id))
            return cur.rowcount > 0
        finally:
            conn.close()

"""Conversation persistence: encrypted message history per interface,
with a rolling window fed back into the prompt as short-term memory."""

from __future__ import annotations

import time
from pathlib import Path

from jarvis.core import db
from jarvis.core.crypto import Vault

PURPOSE = "memory"


class ConversationLog:
    def __init__(self, db_path: str | Path, vault: Vault):
        self.db_path = db_path
        self.vault = vault

    def get_or_create(self, interface: str, external_id: str | None = None) -> int:
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM conversations WHERE interface=? AND external_id IS ?"
                " ORDER BY id DESC LIMIT 1",
                (interface, external_id),
            ).fetchone()
            if row:
                return row["id"]
            cur = conn.execute(
                "INSERT INTO conversations (interface, external_id, started_at, last_at)"
                " VALUES (?, ?, ?, ?)",
                (interface, external_id, time.time(), time.time()),
            )
            return cur.lastrowid
        finally:
            conn.close()

    def append(self, conversation_id: int, role: str, content: str,
               tier: str | None = None) -> None:
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content_enc, tier, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (conversation_id, role, self.vault.encrypt(PURPOSE, content),
                 tier, time.time()),
            )
            conn.execute(
                "UPDATE conversations SET last_at=? WHERE id=?",
                (time.time(), conversation_id),
            )
        finally:
            conn.close()

    def recent(self, conversation_id: int, limit: int = 12) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT role, content_enc FROM messages WHERE conversation_id=?"
                " ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"role": r["role"],
             "content": self.vault.decrypt_text(PURPOSE, r["content_enc"])}
            for r in reversed(rows)
        ]

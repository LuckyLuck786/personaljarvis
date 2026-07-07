"""Encrypted hybrid memory store — the RAG pipeline.

    ingest:  chunk → embed (hub-local) → LanceDB (vectors only)
                                       → SQLite (Fernet-encrypted content)
    search:  vector top-(4k) candidates → decrypt → BM25 keyword scoring
             → reciprocal-rank-fusion → top k

Deliberate calls:
  * LanceDB holds vectors + ids ONLY. All text lives encrypted in SQLite,
    so at-rest exposure is embeddings (which leak topic, not content) and
    Fernet tokens. Documented trade-off vs. an on-disk FTS index: keyword
    recall is limited to what the vector prefilter surfaces. At personal
    scale (tens of thousands of chunks) the 4x oversample makes this a
    non-issue in practice; LUKS underneath covers the vectors.
  * BM25 is ~40 lines of stdlib math over ≤ a few dozen candidates per
    query — no search server, no extra RAM.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from jarvis.core import db
from jarvis.core.config import Config
from jarvis.core.crypto import Vault
from jarvis.core.logging import get_logger
from jarvis.memory.chunking import chunk_text

log = get_logger(__name__)

PURPOSE = "memory"
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _bm25(query: list[str], docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> list[float]:
    n = len(docs)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: dict[str, int] = {}
    for term in set(query):
        df[term] = sum(1 for d in docs if term in d)
    scores = []
    for d in docs:
        dl = len(d) or 1
        s = 0.0
        for term in query:
            tf = d.count(term)
            if tf == 0:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


@dataclass
class SearchResult:
    chunk_id: str
    doc_id: int
    text: str
    score: float
    kind: str
    source: str
    tags: str
    ts: float


class MemoryStore:
    def __init__(self, cfg: Config, vault: Vault, embedder):
        self.cfg = cfg
        self.vault = vault
        self.embedder = embedder
        self.lance_dir: Path = cfg.data_dir / "lancedb"
        self._lance = None
        self._table = None
        self._lock = asyncio.Lock()

    # -- lance plumbing -------------------------------------------------------

    def _lance_db(self):
        if self._lance is None:
            import lancedb  # deferred: pyarrow import costs RAM; only pay when memory is used

            self._lance = lancedb.connect(str(self.lance_dir))
        return self._lance

    def _open_or_create_table(self, dim: int):
        if self._table is None:
            ldb = self._lance_db()
            if "chunks" in ldb.table_names():
                self._table = ldb.open_table("chunks")
            else:
                import pyarrow as pa

                schema = pa.schema(
                    [
                        pa.field("id", pa.string()),
                        pa.field("doc_id", pa.int64()),
                        pa.field("ts", pa.float64()),
                        pa.field("vector", pa.list_(pa.float32(), dim)),
                    ]
                )
                self._table = ldb.create_table("chunks", schema=schema)
        return self._table

    # -- ingest ---------------------------------------------------------------

    async def ingest(
        self,
        text: str,
        kind: str,
        source: str,
        tags: tuple[str, ...] = (),
        meta: dict | None = None,
        ts: float | None = None,
    ) -> int | None:
        """Returns the new doc id, or None for empty input."""
        chunks = chunk_text(text)
        if not chunks:
            return None
        vectors = await self.embedder.embed(chunks)
        ts = ts or time.time()

        async with self._lock:
            conn = db.connect(self.cfg.db_path)
            try:
                cur = conn.execute(
                    "INSERT INTO memory_docs (kind, source, content_enc, meta, tags, ts)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, source, self.vault.encrypt(PURPOSE, text),
                     json.dumps(meta or {}), ",".join(tags), ts),
                )
                doc_id = cur.lastrowid
                rows = []
                for seq, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    chunk_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO memory_chunks (id, doc_id, seq, content_enc, ts)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (chunk_id, doc_id, seq, self.vault.encrypt(PURPOSE, chunk), ts),
                    )
                    rows.append({"id": chunk_id, "doc_id": doc_id, "ts": ts,
                                 "vector": [float(v) for v in vec]})
            finally:
                conn.close()
            self._open_or_create_table(dim=len(vectors[0])).add(rows)
        log.info("memory_ingested", doc_id=doc_id, kind=kind, source=source, chunks=len(chunks))
        return doc_id

    # -- search ---------------------------------------------------------------

    async def search(self, query: str, k: int = 6, kinds: list[str] | None = None) -> list[SearchResult]:
        if self._table is None:
            ldb = self._lance_db()
            if "chunks" not in ldb.table_names():
                return []  # nothing ingested yet
            self._table = ldb.open_table("chunks")

        qvec = (await self.embedder.embed([query]))[0]
        oversample = max(4 * k, 24)
        hits = self._table.search([float(v) for v in qvec]).limit(oversample).to_list()
        if not hits:
            return []

        conn = db.connect(self.cfg.db_path)
        try:
            candidates = []
            for rank, h in enumerate(hits):
                row = conn.execute(
                    "SELECT c.id, c.doc_id, c.content_enc, d.kind, d.source, d.tags, c.ts"
                    " FROM memory_chunks c JOIN memory_docs d ON d.id = c.doc_id"
                    " WHERE c.id = ?",
                    (h["id"],),
                ).fetchone()
                if row is None:
                    continue  # vector row orphaned by a delete; ignore
                if kinds and row["kind"] not in kinds:
                    continue
                candidates.append((rank, row, self.vault.decrypt_text(PURPOSE, row["content_enc"])))
        finally:
            conn.close()
        if not candidates:
            return []

        # keyword scoring over the decrypted candidate set, then RRF fusion
        qtok = _tokens(query)
        bm25 = _bm25(qtok, [_tokens(text) for _, _, text in candidates])
        bm25_rank = {i: r for r, i in
                     enumerate(sorted(range(len(candidates)), key=lambda i: -bm25[i]))}

        fused: list[tuple[float, SearchResult]] = []
        for i, (vec_rank, row, text) in enumerate(candidates):
            score = 1.0 / (60 + vec_rank) + 1.0 / (60 + bm25_rank[i])
            fused.append(
                (score, SearchResult(row["id"], row["doc_id"], text, round(score, 6),
                                     row["kind"], row["source"], row["tags"], row["ts"]))
            )
        fused.sort(key=lambda t: -t[0])
        return [r for _, r in fused[:k]]

    # -- structured access ----------------------------------------------------

    def recent_docs(self, limit: int = 20, kinds: list[str] | None = None) -> list[dict]:
        conn = db.connect(self.cfg.db_path)
        try:
            if kinds:
                marks = ",".join("?" for _ in kinds)
                rows = conn.execute(
                    f"SELECT id, kind, source, content_enc, tags, ts FROM memory_docs"
                    f" WHERE kind IN ({marks}) ORDER BY ts DESC LIMIT ?",
                    (*kinds, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, kind, source, content_enc, tags, ts FROM memory_docs"
                    " ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {"id": r["id"], "kind": r["kind"], "source": r["source"],
                 "tags": r["tags"], "ts": r["ts"],
                 "text": self.vault.decrypt_text(PURPOSE, r["content_enc"])}
                for r in rows
            ]
        finally:
            conn.close()

    def timeline(self, since: float, until: float | None = None,
                 kinds: list[str] | None = None, limit: int = 100,
                 preview_chars: int = 300) -> list[dict]:
        """Chronological view of everything captured/remembered in a window."""
        until = until or time.time()
        conn = db.connect(self.cfg.db_path)
        try:
            sql = ("SELECT id, kind, source, content_enc, tags, ts FROM memory_docs"
                   " WHERE ts >= ? AND ts <= ?")
            params: list = [since, until]
            if kinds:
                sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
                params.extend(kinds)
            sql += " ORDER BY ts ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [
            {"id": r["id"], "kind": r["kind"], "source": r["source"],
             "tags": r["tags"], "ts": r["ts"],
             "preview": self.vault.decrypt_text(PURPOSE, r["content_enc"])[:preview_chars]}
            for r in rows
        ]

    def delete_doc(self, doc_id: int) -> bool:
        conn = db.connect(self.cfg.db_path)
        try:
            cur = conn.execute("DELETE FROM memory_docs WHERE id = ?", (doc_id,))
            deleted = cur.rowcount > 0
        finally:
            conn.close()
        if deleted and self._table is not None:
            self._table.delete(f"doc_id == {int(doc_id)}")
        return deleted

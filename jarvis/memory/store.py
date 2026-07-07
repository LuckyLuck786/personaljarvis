"""Encrypted hybrid memory store — the RAG pipeline.

    ingest:  chunk → embed (hub-local) → SQLite (normalized vector blob)
                                       → SQLite (Fernet-encrypted content)
    search:  brute-force cosine top-(4k) → decrypt → BM25 keyword scoring
             → reciprocal-rank-fusion → top k

Deliberate calls:
  * Vectors live in SQLite as normalized float32 blobs; similarity is a
    pure-Python cosine (== dot product, since normalized). NO LanceDB /
    pyarrow: their native kernels require AVX and SIGILL on pre-2011 CPUs
    (the 2010 Mac Mini), and they're heavy on a 6 GB box. Brute-force is
    O(N·dim) per query — trivial at personal scale (thousands of chunks) and
    it runs on ANY CPU. If you ever store hundreds of thousands of chunks,
    add an ANN index then; you are nowhere near that.
  * Content stays Fernet-encrypted in SQLite; vectors are just numbers.
  * BM25 is ~40 lines of stdlib math over the candidate set — no search
    server, no extra RAM.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import math
import re
import time
import uuid
from array import array
from dataclasses import dataclass

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


def _normalize(vec) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


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
    def __init__(self, cfg: Config, vault: Vault, embedder, graph=None):
        self.cfg = cfg
        self.vault = vault
        self.embedder = embedder
        self.graph = graph          # KnowledgeGraph | None — populated on ingest
        self._lock = asyncio.Lock()

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
                for seq, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    chunk_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO memory_chunks (id, doc_id, seq, content_enc, ts)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (chunk_id, doc_id, seq, self.vault.encrypt(PURPOSE, chunk), ts),
                    )
                    nv = _normalize(vec)
                    conn.execute(
                        "INSERT INTO memory_vectors (chunk_id, doc_id, ts, dim, vec)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (chunk_id, doc_id, ts, len(nv), _pack(nv)),
                    )
            finally:
                conn.close()
        # populate the knowledge graph from the full doc text — best-effort,
        # never let a graph hiccup fail an ingest
        if self.graph is not None:
            try:
                self.graph.ingest_text(text, ts=ts)
            except Exception:
                log.exception("graph_ingest_failed", doc_id=doc_id)
        log.info("memory_ingested", doc_id=doc_id, kind=kind, source=source, chunks=len(chunks))
        return doc_id

    # -- search ---------------------------------------------------------------

    async def search(self, query: str, k: int = 6, kinds: list[str] | None = None) -> list[SearchResult]:
        qvec = _normalize((await self.embedder.embed([query]))[0])
        qn = len(qvec)
        oversample = max(4 * k, 24)

        conn = db.connect(self.cfg.db_path)
        try:
            # brute-force cosine (== dot, since both sides are unit-normalized).
            # heapq keeps only the top oversample, so peak RAM is bounded.
            def scored():
                for r in conn.execute("SELECT chunk_id, vec FROM memory_vectors"):
                    v = _unpack(r["vec"])
                    if len(v) != qn:
                        continue
                    yield (sum(qvec[i] * v[i] for i in range(qn)), r["chunk_id"])

            top = heapq.nlargest(oversample, scored(), key=lambda t: t[0])
            if not top:
                return []
            top_ids = [cid for _, cid in top]

            marks = ",".join("?" for _ in top_ids)
            meta = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT c.id, c.doc_id, c.content_enc, d.kind, d.source, d.tags,"
                    f" c.ts FROM memory_chunks c JOIN memory_docs d ON d.id = c.doc_id"
                    f" WHERE c.id IN ({marks})",
                    top_ids,
                )
            }
        finally:
            conn.close()

        candidates = []
        for rank, cid in enumerate(top_ids):
            row = meta.get(cid)
            if row is None:
                continue  # chunk gone (deleted); ignore
            if kinds and row["kind"] not in kinds:
                continue
            candidates.append((rank, row, self.vault.decrypt_text(PURPOSE, row["content_enc"])))
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

    def backfill_graph(self, batch: int = 500) -> int:
        """Populate the knowledge graph from all existing memory docs (for
        data ingested before the graph existed). Returns docs processed."""
        if self.graph is None:
            return 0
        conn = db.connect(self.cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT content_enc, ts FROM memory_docs ORDER BY id LIMIT ?",
                (batch * 1000,),
            ).fetchall()
        finally:
            conn.close()
        n = 0
        for r in rows:
            try:
                text = self.vault.decrypt_text(PURPOSE, r["content_enc"])
                self.graph.ingest_text(text, ts=r["ts"])
                n += 1
            except Exception:
                log.exception("graph_backfill_doc_failed")
        log.info("graph_backfilled", docs=n)
        return n

    def delete_doc(self, doc_id: int) -> bool:
        # ON DELETE CASCADE removes the chunks and their vectors (FK chain).
        conn = db.connect(self.cfg.db_path)
        try:
            cur = conn.execute("DELETE FROM memory_docs WHERE id = ?", (doc_id,))
            return cur.rowcount > 0
        finally:
            conn.close()

    async def reindex(self) -> int:
        """Re-embed any chunks that have no vector yet — e.g. after migrating
        off LanceDB, or if embeddings were unavailable during an ingest.
        Returns the number of chunks reindexed."""
        conn = db.connect(self.cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT c.id, c.doc_id, c.content_enc, c.ts FROM memory_chunks c"
                " LEFT JOIN memory_vectors v ON v.chunk_id = c.id"
                " WHERE v.chunk_id IS NULL"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return 0
        texts = [self.vault.decrypt_text(PURPOSE, r["content_enc"]) for r in rows]
        vectors = await self.embedder.embed(texts)
        conn = db.connect(self.cfg.db_path)
        try:
            for r, vec in zip(rows, vectors):
                nv = _normalize(vec)
                conn.execute(
                    "INSERT OR REPLACE INTO memory_vectors (chunk_id, doc_id, ts, dim, vec)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (r["id"], r["doc_id"], r["ts"], len(nv), _pack(nv)),
                )
        finally:
            conn.close()
        log.info("memory_reindexed", chunks=len(rows))
        return len(rows)

"""Lightweight knowledge graph (entities + relations) over the personal
archive — the "who / what / when" layer from the spec.

Design for the 6 GB hub: NO heavy NLP (spaCy/transformers would blow the RAM
budget). Extraction is a cheap deterministic pass that runs inline on every
ingest:

  * proper-noun runs (Title Case sequences) → candidate people/orgs/places
  * "project <Name>", "the <Name> project" → projects
  * @handle mentions → people

Entities co-occurring in the same document get a `co_occurs` edge whose
weight increments each time — so "who is connected to X" falls out of the
accumulated weights. Entity NAMES only are stored (not the surrounding
content), and names are low-sensitivity; the content itself stays encrypted
in memory_docs.

Higher-quality *typed* extraction can be layered in during nightly
consolidation via the router — this module exposes `merge_entities()` for
that — but the base graph never depends on an LLM being reachable.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from jarvis.core import db
from jarvis.core.logging import get_logger

log = get_logger(__name__)

# A run of 2+ consecutive Title-Case words on the SAME line (no lowercase
# joiners like "and", no newlines) — precision over recall. Single bare
# capitalized words are too noisy to treat as entities without real NLP
# (which the 6 GB hub can't afford), so we deliberately require multi-word
# names + explicit project/handle patterns. Documented tradeoff: we may miss
# a first-name-only mention, but we don't fill the graph with "Running",
# "Total", "Understood", etc.
_PROPER = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:[ \t]+[A-Z][a-zA-Z0-9]+){1,4})\b")
_PROJECT = re.compile(r"(?:project\s+([A-Z][\w-]+)|(?:the\s+)?([A-Z][\w-]+)\s+project)", re.I)
_HANDLE = re.compile(r"(?<!\w)@([a-zA-Z0-9_]{2,30})")

# leading words that, even in a multi-word run, signal a non-entity phrase
# (imperative openers, JARVIS-isms). Applied to the FIRST word of a run.
_STOP = {
    "I", "The", "A", "An", "This", "That", "You", "Your", "My", "It", "We",
    "Operator", "Jarvis", "JARVIS", "Sir", "Note", "Reminder", "Remember",
    "Please", "Thanks", "Thank", "Hello", "Hi", "Today", "Tomorrow", "Yesterday",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "OK", "Okay", "Yes", "No",
    "Are", "Is", "Was", "Will", "Would", "Should", "Could", "Given", "Used",
    "Running", "Free", "Total", "Summarize", "Understood", "Reply", "Here",
    "There", "What", "When", "Where", "Who", "Why", "How", "Let", "Do", "Does",
    "Phase", "Research", "Then", "Also", "And", "But", "If", "Set", "Get",
}
# drop a run if ANY word is one of these connector/filler tokens that only
# appear mid-phrase, not in real names
_BAD_WORD = {"And", "Or", "But", "The", "Is", "Are", "Was", "To", "Of", "For"}
MIN_LEN = 2
MAX_LEN = 60


@dataclass
class Extraction:
    people: set[str]
    projects: set[str]
    other: set[str]

    def all_named(self) -> list[tuple[str, str]]:
        return ([(n, "person") for n in self.people]
                + [(n, "project") for n in self.projects]
                + [(n, "thing") for n in self.other])


def extract_entities(text: str) -> Extraction:
    people: set[str] = set()
    projects: set[str] = set()
    other: set[str] = set()

    for m in _HANDLE.finditer(text):
        people.add(m.group(1))
    for m in _PROJECT.finditer(text):
        name = (m.group(1) or m.group(2) or "").strip()
        if MIN_LEN <= len(name) <= MAX_LEN and name not in _STOP:
            projects.add(name)

    for m in _PROPER.finditer(text):
        name = m.group(1).strip()
        if not (MIN_LEN <= len(name) <= MAX_LEN):
            continue
        words = name.split()
        if words[0] in _STOP:
            continue
        if any(w in _BAD_WORD for w in words):
            # trim a trailing connector run to salvage the leading name, else skip
            trimmed = []
            for w in words:
                if w in _BAD_WORD:
                    break
                trimmed.append(w)
            if len(trimmed) < 2:
                continue
            name = " ".join(trimmed)
        if name in projects:
            continue
        other.add(name)

    # We only keep multi-word proper names (+ projects + @handles). person-vs-
    # org typing isn't guessed without an LLM — optional typed refinement can
    # be layered in during nightly consolidation; the base graph stays honest
    # and heuristic.
    return Extraction(people=people, projects=projects, other=other)


class KnowledgeGraph:
    def __init__(self, db_path: str | Path):
        self.db_path = db_path

    def _upsert_entity(self, conn, name: str, kind: str, ts: float) -> int:
        row = conn.execute(
            "SELECT id, kind FROM entities WHERE name=? AND kind=?", (name, kind)
        ).fetchone()
        if row:
            conn.execute("UPDATE entities SET last_seen=? WHERE id=?", (ts, row["id"]))
            return row["id"]
        cur = conn.execute(
            "INSERT INTO entities (name, kind, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?)", (name, kind, ts, ts),
        )
        return cur.lastrowid

    def _add_cooccurrence(self, conn, a: int, b: int, ts: float) -> None:
        lo, hi = sorted((a, b))
        row = conn.execute(
            "SELECT id, weight FROM relations WHERE src=? AND dst=? AND rel='co_occurs'",
            (lo, hi),
        ).fetchone()
        if row:
            conn.execute("UPDATE relations SET weight=weight+1, ts=? WHERE id=?",
                         (ts, row["id"]))
        else:
            conn.execute(
                "INSERT INTO relations (src, dst, rel, weight, ts)"
                " VALUES (?, ?, 'co_occurs', 1, ?)", (lo, hi, ts),
            )

    def ingest_text(self, text: str, ts: float | None = None) -> int:
        """Extract entities from one doc and update the graph. Returns the
        number of distinct entities seen. Best-effort: never raises into the
        memory ingest path."""
        ts = ts or time.time()
        named = extract_entities(text).all_named()
        if not named:
            return 0
        conn = db.connect(self.db_path)
        try:
            conn.execute("BEGIN")
            try:
                ids = []
                seen = set()
                for name, kind in named:
                    key = name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    ids.append(self._upsert_entity(conn, name, kind, ts))
                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        self._add_cooccurrence(conn, ids[i], ids[j], ts)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return len(ids)
        finally:
            conn.close()

    # -- queries ---------------------------------------------------------------

    def neighbors(self, name: str, limit: int = 15) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            ent = conn.execute(
                "SELECT id, name, kind FROM entities WHERE name=? COLLATE NOCASE"
                " ORDER BY last_seen DESC LIMIT 1", (name,),
            ).fetchone()
            if ent is None:
                return []
            rows = conn.execute(
                "SELECT e.name, e.kind, r.weight FROM relations r"
                " JOIN entities e ON e.id = CASE WHEN r.src=? THEN r.dst ELSE r.src END"
                " WHERE (r.src=? OR r.dst=?) AND r.rel='co_occurs'"
                " ORDER BY r.weight DESC LIMIT ?",
                (ent["id"], ent["id"], ent["id"], limit),
            ).fetchall()
            return [{"name": r["name"], "kind": r["kind"], "weight": r["weight"]}
                    for r in rows]
        finally:
            conn.close()

    def top_entities(self, limit: int = 20, kind: str | None = None) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            if kind:
                rows = conn.execute(
                    "SELECT e.name, e.kind, COALESCE(SUM(r.weight),0)+"
                    " (SELECT COUNT(*) FROM relations rr WHERE rr.src=e.id OR rr.dst=e.id)"
                    " AS degree FROM entities e"
                    " LEFT JOIN relations r ON r.src=e.id OR r.dst=e.id"
                    " WHERE e.kind=? GROUP BY e.id ORDER BY degree DESC, e.last_seen DESC"
                    " LIMIT ?", (kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT e.name, e.kind,"
                    " (SELECT COUNT(*) FROM relations rr WHERE rr.src=e.id OR rr.dst=e.id)"
                    " AS degree FROM entities e"
                    " GROUP BY e.id ORDER BY degree DESC, e.last_seen DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [{"name": r["name"], "kind": r["kind"], "degree": r["degree"]}
                    for r in rows]
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = db.connect(self.db_path)
        try:
            e = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            r = conn.execute("SELECT COUNT(*) AS n FROM relations").fetchone()["n"]
        finally:
            conn.close()
        return {"entities": e, "relations": r}

-- Phase 1: memory + conversations.
-- Content columns are Fernet-encrypted by the app (purpose "memory") —
-- SQLite never sees plaintext. Vectors live in LanceDB keyed by chunk id;
-- LanceDB stores NO text, only vectors + ids, so all content sits in one
-- encrypted place.

CREATE TABLE memory_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- 'note' | 'chat' | 'web' | 'capture' | ...
    source      TEXT NOT NULL,            -- 'telegram' | 'cli' | collector name | url
    content_enc TEXT NOT NULL,            -- Fernet token of the full text
    meta        TEXT NOT NULL DEFAULT '{}',
    tags        TEXT NOT NULL DEFAULT '', -- comma-separated; privacy tags live here
    ts          REAL NOT NULL
);
CREATE INDEX idx_memory_docs_ts ON memory_docs (ts);
CREATE INDEX idx_memory_docs_kind ON memory_docs (kind);

CREATE TABLE memory_chunks (
    id          TEXT PRIMARY KEY,         -- uuid; also the LanceDB key
    doc_id      INTEGER NOT NULL REFERENCES memory_docs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    content_enc TEXT NOT NULL,            -- Fernet token of the chunk text
    ts          REAL NOT NULL
);
CREATE INDEX idx_memory_chunks_doc ON memory_chunks (doc_id);

CREATE TABLE conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    interface  TEXT NOT NULL,             -- 'telegram' | 'cli' | 'voice' | 'web'
    external_id TEXT,                     -- e.g. telegram chat id
    started_at REAL NOT NULL,
    last_at    REAL NOT NULL
);
CREATE INDEX idx_conversations_iface ON conversations (interface, external_id);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,        -- 'user' | 'assistant'
    content_enc     TEXT NOT NULL,
    tier            TEXT,                 -- which model tier answered (assistant rows)
    ts              REAL NOT NULL
);
CREATE INDEX idx_messages_conv ON messages (conversation_id, id);

-- Lightweight knowledge graph. Schema lands now; population begins with the
-- Phase 2 capture pipeline (entity extraction). Not used before then.
CREATE TABLE entities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- 'person' | 'project' | 'place' | ...
    meta       TEXT NOT NULL DEFAULT '{}',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    UNIQUE (name, kind)
);
CREATE TABLE relations (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    src      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel      TEXT NOT NULL,
    dst      INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    weight   REAL NOT NULL DEFAULT 1.0,
    meta     TEXT NOT NULL DEFAULT '{}',
    ts       REAL NOT NULL
);

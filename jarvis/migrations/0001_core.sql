-- Phase 0 core infrastructure schema.
-- NOTE: no BEGIN/COMMIT here — the migration runner wraps each file in a
-- transaction together with its schema_migrations bookkeeping row.

-- Durable message bus: append-only log + per-consumer cursors.
-- Fanout pub/sub with at-least-once delivery; consumers advance their own
-- cursor after successful handling.
CREATE TABLE bus_messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    topic   TEXT NOT NULL,
    payload TEXT NOT NULL,          -- JSON
    actor   TEXT NOT NULL DEFAULT 'system',
    ts      REAL NOT NULL
);
CREATE INDEX idx_bus_messages_topic_id ON bus_messages (topic, id);
CREATE INDEX idx_bus_messages_ts ON bus_messages (ts);

CREATE TABLE bus_cursors (
    consumer TEXT NOT NULL,
    topic    TEXT NOT NULL,
    last_id  INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (consumer, topic)
);

-- Per-consumer failure tracking; after bus.max_attempts the message is
-- copied to bus_dead and the cursor advances past it.
CREATE TABLE bus_failures (
    consumer   TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (consumer, message_id)
);

CREATE TABLE bus_dead (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer   TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    topic      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    last_error TEXT,
    ts         REAL NOT NULL
);

-- Append-only, hash-chained audit log. Every action JARVIS takes lands
-- here: hash = sha256(prev_hash | canonical_entry), so tampering with any
-- row breaks the chain from that point on.
CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    actor     TEXT NOT NULL,        -- 'operator' | 'jarvis' | 'system' | interface name
    action    TEXT NOT NULL,        -- e.g. 'control.pause', 'tool.shell_exec'
    params    TEXT NOT NULL,        -- JSON
    outcome   TEXT NOT NULL,        -- 'ok' | 'denied' | 'error:<...>'
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);

-- Small key/value system state: kill switch, degraded-mode flags, etc.
CREATE TABLE system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Node reachability history (MacBook awake? hub Ollama up?). The proactive
-- engine reads transitions from here / the bus for anomaly alerts.
CREATE TABLE node_status (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node       TEXT NOT NULL,
    status     TEXT NOT NULL,       -- 'up' | 'down'
    latency_ms REAL,
    detail     TEXT,                -- JSON (e.g. model list or error)
    ts         REAL NOT NULL
);
CREATE INDEX idx_node_status_node_ts ON node_status (node, ts);

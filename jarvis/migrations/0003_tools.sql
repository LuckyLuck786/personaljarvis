-- Phase 3: tasks/reminders + destructive-action confirmations.

CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    due_ts       REAL,                    -- NULL = no deadline
    remind       INTEGER NOT NULL DEFAULT 0,
    reminded_at  REAL,                    -- set when the reminder fired
    status       TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'done' | 'cancelled'
    created_ts   REAL NOT NULL,
    completed_ts REAL
);
CREATE INDEX idx_tasks_status_due ON tasks (status, due_ts);

-- Destructive tool calls park here until the operator confirms by token.
CREATE TABLE pending_confirmations (
    token      TEXT PRIMARY KEY,
    tool       TEXT NOT NULL,
    args       TEXT NOT NULL,             -- JSON
    interface  TEXT NOT NULL,
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'  -- 'pending' | 'executed' | 'expired' | 'denied'
);

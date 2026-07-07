-- Phase 4: proactive engine — scheduled jobs, follow-ups, daily summaries.

-- Scheduled jobs. next_run is recomputed after each fire from the schedule
-- spec. Survives restarts; the scheduler claims due jobs atomically.
CREATE TABLE scheduled_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,      -- 'morning_digest' | 'nightly_consolidation' | ...
    kind        TEXT NOT NULL,             -- handler key in the proactive engine
    schedule    TEXT NOT NULL,             -- 'daily@HH:MM' | 'everyN:<seconds>'
    enabled     INTEGER NOT NULL DEFAULT 1,
    next_run    REAL NOT NULL,
    last_run    REAL,
    last_status TEXT,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_ts  REAL NOT NULL
);
CREATE INDEX idx_scheduled_jobs_next ON scheduled_jobs (enabled, next_run);

-- Commitments JARVIS extracts from conversation ("I'll email Sam tomorrow").
-- The follow-up job nudges on ones that pass their by_ts still open.
CREATE TABLE followups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,             -- the commitment, encrypted
    source_ts  REAL NOT NULL,             -- when it was said
    by_ts      REAL,                      -- soft deadline if any
    status     TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'done' | 'dismissed' | 'nudged'
    nudged_at  REAL,
    created_ts REAL NOT NULL
);
CREATE INDEX idx_followups_status ON followups (status, by_ts);

-- Durable daily consolidation notes (the "second brain" long-term layer).
CREATE TABLE daily_summaries (
    day        TEXT PRIMARY KEY,          -- 'YYYY-MM-DD' (local)
    summary_enc TEXT NOT NULL,            -- encrypted narrative
    event_count INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL
);

-- Phase 7: self-improvement proposals, sub-agent runs, digital twin.

-- Automation/tool ideas JARVIS proposes from observed patterns. It NEVER
-- self-executes these — the operator reviews and approves. (Prompt-injection
-- + autonomy safety: proposals are suggestions, not actions.)
CREATE TABLE improvement_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,             -- 'automation' | 'tool' | 'habit'
    title       TEXT NOT NULL,
    rationale_enc TEXT NOT NULL,           -- why, grounded in observed patterns
    evidence    TEXT NOT NULL DEFAULT '{}',-- JSON: counts/signals behind it
    status      TEXT NOT NULL DEFAULT 'proposed',  -- proposed|accepted|dismissed
    created_ts  REAL NOT NULL
);
CREATE INDEX idx_proposals_status ON improvement_proposals (status);

-- Dispatched sub-agent tasks (researcher, coder, ...). Runs async on the
-- bus; results land back here and (for research) in memory.
CREATE TABLE agent_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,             -- 'researcher' | 'coder' | 'summarizer'
    objective_enc TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error
    result_enc  TEXT,
    steps       INTEGER NOT NULL DEFAULT 0,
    created_ts  REAL NOT NULL,
    finished_ts REAL
);
CREATE INDEX idx_agent_runs_status ON agent_runs (status);

-- The self-maintaining "digital twin": a rolling personal-context summary,
-- versioned so we keep history of how JARVIS's model of the operator evolves.
CREATE TABLE digital_twin (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_enc TEXT NOT NULL,
    facts_enc   TEXT NOT NULL DEFAULT '',  -- distilled stable facts
    source_count INTEGER NOT NULL DEFAULT 0,
    created_ts  REAL NOT NULL
);

-- M1 foundation tables. Conversation, people, tasks, etc. arrive in later migrations (SPEC §7).

CREATE TABLE recording (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,              -- recorder, app, share, upload
    original_path TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,       -- dedupe
    duration_s    REAL,
    received_at   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new'
);

-- A job is one run of a pipeline (e.g. "process this recording"). Its stages are saved one by one in stage_run.
CREATE TABLE job (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    recording_id INTEGER REFERENCES recording(id),
    payload      TEXT NOT NULL DEFAULT '{}',  -- JSON
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'failed')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    error        TEXT
);
CREATE INDEX job_status ON job(status, id);

CREATE TABLE stage_run (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT,
    output      TEXT,                         -- what the stage saved (usually a file path)
    error       TEXT,
    UNIQUE (job_id, stage)
);

CREATE TABLE schedule_run (
    job_name        TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_attempt_at TEXT,
    status          TEXT
);

CREATE TABLE setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

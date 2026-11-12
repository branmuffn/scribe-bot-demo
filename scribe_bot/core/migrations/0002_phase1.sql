-- Phase 1 data model (SPEC §7, OUTPUT_DESIGN.md v1). Shared contract for M2–M6.
-- Times are ISO 8601 strings. "me" (Brian) is person id 1, created below.
-- Later migrations only add (never drop or rename), so a rollback to older code still works.

ALTER TABLE recording ADD COLUMN title TEXT;            -- from the file name (Recorder title)
ALTER TABLE recording ADD COLUMN vault_path TEXT;       -- Scribe Bot's own copy
ALTER TABLE recording ADD COLUMN archive_path TEXT;     -- Opus archive (D8)
ALTER TABLE recording ADD COLUMN original_deleted_at TEXT;
ALTER TABLE recording ADD COLUMN private INTEGER NOT NULL DEFAULT 0;  -- "private" in the title (DECISIONS 2026-10-19)

CREATE TABLE person (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    aliases        TEXT NOT NULL DEFAULT '[]',   -- JSON list
    area           TEXT NOT NULL DEFAULT 'both' CHECK (area IN ('work', 'personal', 'both')),
    notes          TEXT,
    last_spoken_at TEXT,
    created_at     TEXT NOT NULL
);
INSERT INTO person (id, name, created_at) VALUES (1, 'Brian', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

CREATE TABLE voiceprint (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    embedding    BLOB NOT NULL,                  -- float32 array, normalised
    sample_count INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL
);

CREATE TABLE conversation (
    id              INTEGER PRIMARY KEY,
    recording_id    INTEGER UNIQUE REFERENCES recording(id),
    title           TEXT NOT NULL,
    started_at      TEXT,
    duration_s      REAL,
    type            TEXT CHECK (type IN ('multi', '1:1', 'intro', 'personal', 'voice_note')),
    area            TEXT CHECK (area IN ('work', 'personal')),
    area_confidence REAL,
    summary         TEXT,
    takeaways       TEXT,                         -- JSON list
    private         INTEGER NOT NULL DEFAULT 0,
    processed_by    TEXT,                         -- 'local' or 'account:model'
    status          TEXT NOT NULL DEFAULT 'transcribing',  -- transcribing, transcribed, extracted, done, failed
    deleted_at      TEXT,                         -- Trash (30 days)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Speaker clusters as found in this conversation. cluster = speakers.json number; label = display order (1st speaker = 0).
CREATE TABLE speaker (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    cluster         INTEGER NOT NULL,
    person_id       INTEGER REFERENCES person(id),
    match_score     REAL,                         -- voiceprint similarity, if matched
    confirmed       INTEGER NOT NULL DEFAULT 0,
    talk_seconds    REAL NOT NULL DEFAULT 0,
    clip_path       TEXT,                         -- 5-second sample for the review queue
    UNIQUE (conversation_id, cluster)
);

CREATE TABLE segment (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,             -- line number on the page ("line N")
    start_s         REAL NOT NULL,
    end_s           REAL NOT NULL,
    speaker_id      INTEGER REFERENCES speaker(id),
    text            TEXT NOT NULL,
    confidence      REAL,
    UNIQUE (conversation_id, idx)
);

CREATE TABLE task (
    id                INTEGER PRIMARY KEY,
    conversation_id   INTEGER REFERENCES conversation(id) ON DELETE SET NULL,
    title             TEXT NOT NULL,
    owner_person_id   INTEGER NOT NULL DEFAULT 1 REFERENCES person(id),  -- 1 = me; anyone else = "waiting on"
    due               TEXT,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    area              TEXT CHECK (area IN ('work', 'personal')),
    confidence        REAL,
    inbox             INTEGER NOT NULL DEFAULT 0,
    source_segment_id INTEGER REFERENCES segment(id) ON DELETE SET NULL,
    rated             TEXT CHECK (rated IN ('right', 'wrong')),  -- edits/deletes in the app feed the scores
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE decision (
    id                INTEGER PRIMARY KEY,
    conversation_id   INTEGER REFERENCES conversation(id) ON DELETE CASCADE,
    text              TEXT NOT NULL,
    people            TEXT NOT NULL DEFAULT '[]', -- JSON list of person ids
    source_segment_id INTEGER REFERENCES segment(id) ON DELETE SET NULL,
    rated             TEXT CHECK (rated IN ('right', 'wrong')),
    created_at        TEXT NOT NULL
);

CREATE TABLE person_fact (
    id                INTEGER PRIMARY KEY,
    person_id         INTEGER REFERENCES person(id) ON DELETE CASCADE,
    person_name       TEXT,                       -- as the AI wrote it, until linked to a person
    conversation_id   INTEGER REFERENCES conversation(id) ON DELETE CASCADE,
    type              TEXT NOT NULL DEFAULT 'other' CHECK (type IN ('birthday', 'family', 'event', 'preference', 'work', 'other')),
    value             TEXT NOT NULL,
    date              TEXT,
    source_segment_id INTEGER REFERENCES segment(id) ON DELETE SET NULL,
    confidence        REAL,
    confirmed         INTEGER,                    -- NULL = not reviewed, 1 = yes, 0 = rejected
    created_at        TEXT NOT NULL
);

CREATE TABLE glossary_term (
    id         INTEGER PRIMARY KEY,
    term       TEXT NOT NULL UNIQUE,
    variants   TEXT NOT NULL DEFAULT '[]',       -- JSON list of mis-hearings
    approved   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Review queue: speakers to confirm, inbox items, cloud approvals, glossary suggestions.
CREATE TABLE review_item (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('speaker', 'inbox', 'approval', 'glossary')),
    conversation_id INTEGER REFERENCES conversation(id) ON DELETE CASCADE,
    ref_id          INTEGER,                      -- speaker / task / glossary_term id
    payload         TEXT NOT NULL DEFAULT '{}',   -- JSON
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dismissed')),
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE TABLE capture (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('voice', 'text', 'link', 'image')),
    payload_path TEXT,
    text         TEXT,
    created_at   TEXT NOT NULL,
    processed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE account (
    id              INTEGER PRIMARY KEY,
    provider        TEXT NOT NULL,                -- anthropic, google, openai
    nickname        TEXT NOT NULL,
    owner           TEXT NOT NULL,                -- e.g. you, a family member
    type            TEXT NOT NULL CHECK (type IN ('free', 'credits', 'paid')),
    monthly_cap_usd REAL,
    expires_at      TEXT,
    training_policy TEXT NOT NULL,                -- e.g. "no training (API default)", "may train"
    may_train       INTEGER NOT NULL DEFAULT 0,   -- blocked for real data (rule 2)
    enabled         INTEGER NOT NULL DEFAULT 1,
    secret_ref      TEXT NOT NULL,                -- Keychain service name only, never the key
    created_at      TEXT NOT NULL
);
INSERT INTO account (id, provider, nickname, owner, type, monthly_cap_usd, training_policy, secret_ref, created_at)
VALUES (1, 'anthropic', 'Brian personal', 'Brian', 'paid', 10, 'API: no training on content by default',
        'scribe-bot.anthropic', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

-- Data Ledger: everything that left the M14.
CREATE TABLE ledger_entry (
    id              INTEGER PRIMARY KEY,
    time            TEXT NOT NULL,
    account_id      INTEGER REFERENCES account(id),
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    task            TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversation(id) ON DELETE SET NULL,
    sent_summary    TEXT NOT NULL,                -- what was sent, described (never the content)
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL NOT NULL DEFAULT 0,
    training_policy TEXT,
    status          TEXT NOT NULL DEFAULT 'ok'    -- ok, failed, blocked
);
CREATE INDEX ledger_time ON ledger_entry(time);

CREATE TABLE notification (
    id         INTEGER PRIMARY KEY,
    channel    TEXT NOT NULL CHECK (channel IN ('bot', 'alerts')),  -- #scribe-bot, #scribe-alerts
    title      TEXT NOT NULL,                     -- titles, counts, links only; never content
    link       TEXT,
    status     TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'held')),
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE TABLE backup_run (
    id          INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('backup', 'restore_test')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    bytes       INTEGER,
    status      TEXT NOT NULL DEFAULT 'running'
);

-- Keyword search (SPEC §3.6). Rows are (kind, ref_id, conversation_id, text); kept in sync by the code that writes.
CREATE VIRTUAL TABLE search_index USING fts5(kind UNINDEXED, ref_id UNINDEXED, conversation_id UNINDEXED, text,
                                             tokenize = 'porter unicode61');

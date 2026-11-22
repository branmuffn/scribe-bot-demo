-- Merge before processing (Brian, 2026-11-21): several waiting recordings become one new recording, processed once.
-- Each part keeps where it starts in the joined audio, so the transcript can say which recording it came from.
-- The originals get status 'merged' (kept, hidden from the Inbox); Unmerge puts them back while nothing has run.
CREATE TABLE recording_part (
    id           INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recording(id) ON DELETE CASCADE,   -- the joined recording
    source_id    INTEGER NOT NULL REFERENCES recording(id),                     -- one of the originals
    idx          INTEGER NOT NULL,
    offset_s     REAL NOT NULL,
    duration_s   REAL NOT NULL,
    title        TEXT,
    started_at   TEXT,
    prev_status  TEXT NOT NULL
);
CREATE INDEX recording_part_rec ON recording_part(recording_id);
-- the AI may now guess School and Other too (Brian, 2026-11-21)
PRAGMA writable_schema = ON;
UPDATE sqlite_master SET sql = replace(sql, 'CHECK (area_guess IN (''work'', ''personal''))', 'CHECK (area_guess IN (''work'', ''personal'', ''school'', ''other''))')
 WHERE type = 'table' AND name = 'conversation';
PRAGMA writable_schema = OFF;

-- Brian, 2026-11-21: after Scribe Bot deletes a recording's audio (30 days in Trash), it asks whether to delete it from
-- the Pixel (Recorder) / M4 (Voice Memos) too, and keeps a checklist. status: ask (pop-up not answered yet), todo (on
-- the checklist), done (ticked: deleted there), kept (keep it on the device).
CREATE TABLE device_cleanup (
    id           INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL UNIQUE REFERENCES recording(id),
    device       TEXT NOT NULL,              -- pixel, m4, other
    title        TEXT NOT NULL,
    recorded_at  TEXT,
    duration_s   REAL,
    status       TEXT NOT NULL DEFAULT 'ask' CHECK (status IN ('ask', 'todo', 'done', 'kept')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX device_cleanup_status ON device_cleanup(status);

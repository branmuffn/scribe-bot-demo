-- T1 (Brian, 2026-11-20): a quick look at each waiting recording, made on the M14 from its first 2 minutes (no AI,
-- nothing leaves the M14). A Recorder .txt that syncs later wins on the Inbox (T2); processing replaces both.
CREATE TABLE recording_peek (
    recording_id INTEGER PRIMARY KEY REFERENCES recording(id) ON DELETE CASCADE,
    text         TEXT,             -- what was said in the first 2 minutes (speech-to-text, unedited)
    error        TEXT,             -- why no peek could be made (unreadable file...); not retried
    made_at      TEXT NOT NULL
);

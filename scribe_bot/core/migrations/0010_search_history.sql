-- Rev 2: recent searches, kept on the M14 only. "Remember my searches" can be turned off (that clears them too).
CREATE TABLE search_history (
    q       TEXT PRIMARY KEY COLLATE NOCASE,   -- as typed; "School" and "school" are one entry
    last_at TEXT NOT NULL
);
CREATE INDEX search_history_last ON search_history(last_at);

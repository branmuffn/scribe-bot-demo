-- People merge suggestions (Brian, 2026-11-25): "Don't merge" remembers that two people are different (until NULL);
-- ✕ hides the suggestion for 30 days (until = that day). a < b (person ids). Undo deletes the row.
CREATE TABLE person_not_same (
    id         INTEGER PRIMARY KEY,
    a          INTEGER NOT NULL,
    b          INTEGER NOT NULL,
    until      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX person_not_same_ab ON person_not_same(a, b);

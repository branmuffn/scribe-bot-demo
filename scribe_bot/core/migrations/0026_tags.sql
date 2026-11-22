-- M4 A (Brian picked, 2026-11-20): free tags on talks; the AI only suggests tags that already exist.
CREATE TABLE tag (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE conversation_tag (
    conversation_id INTEGER NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    by_ai           INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (conversation_id, tag_id)
);
CREATE INDEX conversation_tag_tag ON conversation_tag(tag_id);

-- Undo bar: each undoable action saves the previous values of the rows it changes; Undo writes them back.
CREATE TABLE undo_action (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,                     -- what the bar says, e.g. Marked "Send the deck" done
    snapshot   TEXT NOT NULL,                     -- JSON: [{"table", "id", "before": {column: value}}]
    created    TEXT NOT NULL DEFAULT '[]',        -- JSON: [{"table", "id"}] rows the action added (removed on undo if unused)
    created_at TEXT NOT NULL,
    undone_at  TEXT
);

-- Undo for Trash › Delete forever (Brian, 2026-11-25: "what if I do need to undo it"). Delete forever now sets
-- purge_at (when the Undo note can no longer bring it back) and the talk leaves Trash at once; the real delete runs
-- after that time (trash.purge_due). Undo clears purge_at, so the talk is back in Trash as it was.
ALTER TABLE conversation ADD COLUMN purge_at TEXT;

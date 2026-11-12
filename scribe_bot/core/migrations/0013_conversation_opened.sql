-- Rev 2: a "New" badge on finished conversations until Brian opens one. Everything already there counts as opened.
ALTER TABLE conversation ADD COLUMN opened_at TEXT;
UPDATE conversation SET opened_at = coalesce(updated_at, created_at);

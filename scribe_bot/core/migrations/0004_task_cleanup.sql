-- Weekly task cleanup (nothing is deleted): finished tasks are archived out of the Done list; old open tasks get a
-- "Still need this?" flag until Brian answers.
ALTER TABLE task ADD COLUMN archived_at TEXT;
ALTER TABLE task ADD COLUMN stale_at TEXT;

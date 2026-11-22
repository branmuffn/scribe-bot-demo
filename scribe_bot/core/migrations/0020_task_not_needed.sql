-- "No longer needed" (Brian 2026-11-20, N1): a task closed without being done (e.g. the event passed). It keeps
-- status 'done' so every open list hides it, but closed_as marks it, so it never counts as finished. NULL = really done.
ALTER TABLE task ADD COLUMN closed_as TEXT CHECK (closed_as IN ('not_needed'));
ALTER TABLE task ADD COLUMN closed_note TEXT;

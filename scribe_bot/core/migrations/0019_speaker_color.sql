-- A speaker's color in one conversation (Brian 2026-10-22: per conversation, not tied to the person). NULL = automatic.
ALTER TABLE speaker ADD COLUMN color TEXT;

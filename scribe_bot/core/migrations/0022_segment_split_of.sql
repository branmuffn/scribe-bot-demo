-- Fix who said what (2026-11-20): a line split by a fix remembers the line it came from, so its pieces join back up
-- when they end up said by the same person again, while lines that were separate from the start stay separate.
ALTER TABLE segment ADD COLUMN split_of INTEGER;

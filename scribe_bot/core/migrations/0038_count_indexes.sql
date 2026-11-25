-- R5-19 (2026-11-25): every page counts the Inbox's older recordings and the talks in Trash for its badges. Without an
-- index each count read the whole table (about 3 ms a page at 20,000 talks on a fast machine, more on the M14).
-- Indexes only: no data changes.
CREATE INDEX IF NOT EXISTS recording_status ON recording(status);
CREATE INDEX IF NOT EXISTS conversation_trashed ON conversation(deleted_at) WHERE deleted_at IS NOT NULL;

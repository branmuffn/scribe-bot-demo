-- F80 (2026-11-26): two reads run on every page and went through a whole table each time. Indexes only: no data changes.
-- 1. The action the Undo bar offers (one from the last 15 seconds). Undo actions are kept, so the read went through
--    every one ever saved, back to front, and through each one's saved rows on the way (20 ms a page at 3,000 actions
--    on a fast machine, growing week by week).
-- 2. Talks deleted forever whose Undo time is over (trash.purge_due). purge_at sits after the AI notes in each row, so
--    every talk's notes were read to reach it (3 to 4 ms a page at 3,000 talks on a fast machine, more on the M14).
CREATE INDEX IF NOT EXISTS undo_action_recent ON undo_action(created_at);
CREATE INDEX IF NOT EXISTS conversation_purge ON conversation(purge_at) WHERE purge_at IS NOT NULL;

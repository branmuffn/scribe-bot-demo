-- Big events (Brian, 2026-11-22): in talks with 6+ voices, unnamed voices that talked under 2 minutes are no longer
-- asked about ("Don't know"); they can still be named on the talk page. New recordings do the same (pipeline/recording.py).
UPDATE speaker SET unknown = 1 WHERE person_id IS NULL AND talk_seconds < 120 AND conversation_id IN
  (SELECT conversation_id FROM speaker GROUP BY conversation_id HAVING count(*) >= 6);
UPDATE review_item SET status = 'dismissed', resolved_at = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE kind = 'speaker' AND status = 'open'
  AND ref_id IN (SELECT id FROM speaker WHERE unknown = 1);

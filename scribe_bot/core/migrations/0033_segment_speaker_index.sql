-- A talk page looked up each speaker's first line by scanning every line of every talk (156 ms on the speed test's
-- big data; Brian, 2026-11-22: "laggy"). An index on the speaker makes it instant.
CREATE INDEX IF NOT EXISTS segment_speaker ON segment(speaker_id, idx);

-- Brian, 2026-11-21 ("yes, delete the audio as well"): 30 days after a talk goes to Trash (or a recording is Skipped
-- in the Inbox), its audio files are deleted too. The recording row stays (with this date) so the same file is
-- never imported again.
ALTER TABLE recording ADD COLUMN audio_deleted_at TEXT;

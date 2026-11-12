-- A person the AI made for an unnamed speaker ("Speaker 2 from Budget sync on Oct 20"): the speaker they stand for.
-- Renaming them names that speaker (and fixes the meeting's text); naming the speaker folds them in. NULL = a real person.
ALTER TABLE person ADD COLUMN stands_for_speaker INTEGER REFERENCES speaker(id) ON DELETE SET NULL;

-- Undo for a deleted person: their row and voiceprint, saved whole (JSON: [{"table", "row"}]).
ALTER TABLE undo_action ADD COLUMN removed TEXT NOT NULL DEFAULT '[]';

-- 1 = Brian picked the meeting type (at approve time or on the conversation), so the AI never switches it to Learning.
ALTER TABLE conversation ADD COLUMN type_chosen INTEGER NOT NULL DEFAULT 0;

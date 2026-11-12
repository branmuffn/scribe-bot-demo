-- Rev 2: auto-file. The AI's Work/Personal guess when it isn't sure enough to file the conversation itself
-- (the conversation stays Not sorted, with the guess offered as the first button).
ALTER TABLE conversation ADD COLUMN area_guess TEXT CHECK (area_guess IN ('work', 'personal'));

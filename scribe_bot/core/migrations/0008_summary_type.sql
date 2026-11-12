-- F6: which meeting-type template the current AI summary was made with (NULL = before templates existed).
ALTER TABLE conversation ADD COLUMN summary_type TEXT;

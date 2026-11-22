-- Q5 B+C (Brian, 2026-11-21): short names for lists (talks and tasks); the full title stays for the talk page and the
-- task pop-up. NULL = not made yet (lists fall back to the full title, wrapped to 2 lines).
ALTER TABLE conversation ADD COLUMN short_title TEXT;
ALTER TABLE task ADD COLUMN short_title TEXT;

-- "Learning" meeting type (talks, classes, podcasts: key ideas, no tasks; Brian 2026-10-20). SQLite can't ALTER a
-- CHECK, so the stored table definition gets the new value (only widens the list; no data changes) and the schema
-- version is bumped so every connection re-reads it (sqlite.org/lang_altertable.html, "other kinds of changes").
PRAGMA writable_schema = ON;
UPDATE sqlite_master SET sql = replace(sql, '''personal'', ''voice_note''))', '''personal'', ''voice_note'', ''learning''))')
 WHERE type = 'table' AND name = 'conversation';
PRAGMA writable_schema = OFF;

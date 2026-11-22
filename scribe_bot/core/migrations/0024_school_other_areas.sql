-- Brian, 2026-11-20: areas are Work, Home, School and Other (stored as work, personal, school, other). Only widens the
-- CHECK lists (no data changes), the same way as 0016; the AI's own guess (area_guess) stays Work or Home.
PRAGMA writable_schema = ON;
UPDATE sqlite_master SET sql = replace(sql, 'area IN (''work'', ''personal'', ''both'')', 'area IN (''work'', ''personal'', ''school'', ''other'', ''both'')')
 WHERE type = 'table' AND name = 'person';
UPDATE sqlite_master SET sql = replace(sql, 'CHECK (area IN (''work'', ''personal''))', 'CHECK (area IN (''work'', ''personal'', ''school'', ''other''))')
 WHERE type = 'table' AND name IN ('conversation', 'task');
PRAGMA writable_schema = OFF;

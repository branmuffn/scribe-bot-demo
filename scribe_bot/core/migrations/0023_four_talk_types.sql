-- Brian, 2026-11-20: talk types are only 1:1, Group, Learning and Personal. Intro becomes 1:1, Voice note becomes Personal.
UPDATE conversation SET type = '1:1' WHERE type = 'intro';
UPDATE conversation SET type = 'personal' WHERE type = 'voice_note';
DELETE FROM setting WHERE key IN ('template:intro', 'template:voice_note');

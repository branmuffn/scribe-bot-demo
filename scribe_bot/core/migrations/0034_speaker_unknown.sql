-- "Don't know" (Brian, 2026-11-22: at events with hundreds of people some voices can never be named): a speaker marked
-- unknown stops counting as "to name" everywhere. Naming them later still works and clears it.
ALTER TABLE speaker ADD COLUMN unknown INTEGER NOT NULL DEFAULT 0;

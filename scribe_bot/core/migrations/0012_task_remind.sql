-- Rev 2: "Remind me" on tasks. At remind_at the task shows under Needs you on Today (and counts in the Today badge);
-- until then it waits in the Snoozed group.
ALTER TABLE task ADD COLUMN remind_at TEXT;
CREATE INDEX task_remind ON task(remind_at) WHERE remind_at IS NOT NULL;

-- Dismiss on 🔔 Notifications: ✕ (or Dismiss all) moves a notification to the Dismissed tab; Restore brings it back.
ALTER TABLE notification ADD COLUMN dismissed_at TEXT;
CREATE INDEX notification_current ON notification(created_at) WHERE dismissed_at IS NULL;

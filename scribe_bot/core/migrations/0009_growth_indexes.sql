-- Rev 2 (growth): indexes for lists that grow with use. The conversation index matches the
-- ORDER BY coalesce(started_at, created_at) every conversation list uses.
CREATE INDEX conversation_when ON conversation(coalesce(started_at, created_at));
CREATE INDEX review_item_status ON review_item(status, created_at);
CREATE INDEX notification_created ON notification(created_at);

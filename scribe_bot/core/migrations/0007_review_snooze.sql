-- "Remind me tomorrow" on Needs you items: hidden from Today (and its count) until this time (UTC ISO).
ALTER TABLE review_item ADD COLUMN snoozed_until TEXT;

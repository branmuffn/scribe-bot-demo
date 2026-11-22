-- Brian, 2026-11-21: no more "Private". A talk without AI notes is just "Transcript only"; Make AI notes works on any.
UPDATE conversation SET private = 0 WHERE private = 1;
UPDATE recording SET private = 0 WHERE private = 1;
UPDATE review_item SET payload = json_set(payload, '$.private', json('false'))
 WHERE status = 'open' AND json_valid(payload) AND json_extract(payload, '$.private') = 1;

-- B7 (Brian, 2026-11-25): something removed on one device leaves the other device's open page too. Each removal is one
-- row here (kind + id, no content), written by the triggers below so every way of removing counts. Pages ask
-- /gone?since=N every 25 s and take the gone rows off the screen. Rows older than a day are dropped as new ones arrive.
CREATE TABLE removal (
    id     INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,          -- talk (moved to Trash), talk_purged (deleted for good), task, person
    ref_id INTEGER NOT NULL,
    at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TRIGGER removal_tidy AFTER INSERT ON removal BEGIN
    DELETE FROM removal WHERE at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day');
END;
CREATE TRIGGER removal_talk_trashed AFTER UPDATE OF deleted_at ON conversation
    WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL BEGIN
    INSERT INTO removal (kind, ref_id) VALUES ('talk', NEW.id);
END;
CREATE TRIGGER removal_talk_purged AFTER DELETE ON conversation BEGIN
    INSERT INTO removal (kind, ref_id) VALUES ('talk_purged', OLD.id);
END;
CREATE TRIGGER removal_task_dropped AFTER UPDATE OF status ON task
    WHEN NEW.status = 'dropped' AND OLD.status IS NOT 'dropped' BEGIN
    INSERT INTO removal (kind, ref_id) VALUES ('task', NEW.id);
END;
CREATE TRIGGER removal_task_deleted AFTER DELETE ON task BEGIN
    INSERT INTO removal (kind, ref_id) VALUES ('task', OLD.id);
END;
CREATE TRIGGER removal_person_deleted AFTER DELETE ON person BEGIN
    INSERT INTO removal (kind, ref_id) VALUES ('person', OLD.id);
END;

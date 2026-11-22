import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scribe_bot.backup import export, recovery, trash
from scribe_bot.core.settings import Settings

REPO = Path(__file__).resolve().parents[2]
T = "2026-10-19T14:00:00+00:00"


def conversation(conn, title="Synthetic 1:1 about the roadmap", deleted_at=None, private=0):
    cid = conn.execute("INSERT INTO conversation (title, started_at, duration_s, type, area, summary, takeaways, private, "
                       "processed_by, deleted_at, created_at, updated_at) VALUES (?, ?, 1500, '1:1', 'work', "
                       "'A made-up summary.', '[\"one\"]', ?, 'brian:claude-sonnet', ?, ?, ?)",
                       (title, T, private, deleted_at, T, T)).lastrowid
    conn.execute("INSERT INTO person (id, name, created_at) VALUES (?, 'Alex Example', ?) ON CONFLICT DO NOTHING", (2, T))
    s1 = conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id, talk_seconds) VALUES (?, 0, 2, 900)", (cid,)).lastrowid
    s2 = conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id, talk_seconds) VALUES (?, 1, 1, 600)", (cid,)).lastrowid
    seg = conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) "
                       "VALUES (?, 0, 0, 5, ?, 'Hello there.')", (cid, s1)).lastrowid
    conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) "
                 "VALUES (?, 1, 65, 70, ?, 'Hi.')", (cid, s2))
    conn.execute("INSERT INTO task (conversation_id, title, due, created_at, updated_at, source_segment_id) "
                 "VALUES (?, 'Send the deck', '2026-10-21', ?, ?, ?)", (cid, T, T, seg))
    conn.execute("INSERT INTO task (conversation_id, title, owner_person_id, created_at, updated_at) "
                 "VALUES (?, 'Share the numbers', 2, ?, ?)", (cid, T, T))
    conn.execute("INSERT INTO decision (conversation_id, text, created_at) VALUES (?, 'Ship v1 first', ?)", (cid, T))
    conn.execute("INSERT INTO person_fact (person_id, conversation_id, value, created_at) VALUES (2, ?, 'Likes hiking', ?)", (cid, T))
    conn.execute("INSERT INTO person_fact (person_id, conversation_id, value, confirmed, created_at) VALUES (2, ?, 'Rejected fact', 0, ?)", (cid, T))
    conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('segment', ?, ?, 'Hello there.')", (seg, cid))
    return cid


def test_export_markdown(conn, tmp_path):
    cfg = Settings(tmp_path)
    cid = conversation(conn)
    assert export.export_all(conn, cfg) == {"files": 1, "written": 1, "removed": 0}
    f = tmp_path / "export" / "conversations" / f"2026-10-19-{cid}-synthetic-1-1-about-the-roadmap.md"
    text = f.read_text()
    for expected in ["# Synthetic 1:1 about the roadmap", "- Length: 25 min", "- People: Alex Example, Brian",
                     "## Summary", "A made-up summary.", "- one", "## You owe", "- [ ] Send the deck · due 2026-10-21",
                     "## They owe you", "- [ ] Share the numbers (Alex Example)", "- Ship v1 first",
                     "- Alex Example: Likes hiking", "**Alex Example** [0:00]: Hello there.", "**Brian** [1:05]: Hi."]:
        assert expected in text
    assert "Rejected fact" not in text
    assert export.export_all(conn, cfg)["written"] == 0  # unchanged files aren't rewritten


def test_export_removes_trashed(conn, tmp_path):
    cfg = Settings(tmp_path)
    cid = conversation(conn)
    export.export_all(conn, cfg)
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = ?", (T, cid))
    assert export.export_all(conn, cfg) == {"files": 0, "written": 0, "removed": 1}


def test_private_is_marked_in_export(conn, tmp_path):
    conversation(conn, private=1)
    export.export_all(conn, Settings(tmp_path))
    assert "**Private** (never left the M14)" in next((tmp_path / "export" / "conversations").glob("*.md")).read_text()


def test_trash_purge_after_30_days(conn):
    now = datetime(2026, 11, 30, tzinfo=timezone.utc)
    old = conversation(conn, "Old", deleted_at=(now - timedelta(days=31)).isoformat(timespec="seconds"))
    recent = conversation(conn, "Recent", deleted_at=(now - timedelta(days=5)).isoformat(timespec="seconds"))
    live = conversation(conn, "Live")
    assert trash.purge(conn, now) == 1
    left = {r[0] for r in conn.execute("SELECT id FROM conversation")}
    assert left == {recent, live}
    assert conn.execute("SELECT count(*) FROM segment WHERE conversation_id = ?", (old,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM search_index WHERE conversation_id = ?", (old,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task WHERE conversation_id IS NULL").fetchone()[0] == 0   # its tasks went (Q1 A)
    assert conn.execute("SELECT count(*) FROM task").fetchone()[0] == 4                                   # the others' stay


def test_recovery_sheet_password_only_on_terminal():
    dests = [{"nickname": "Brian Drive", "remote": "gdrive-brian", "email": "a@example.com"}]
    out = []
    assert recovery.print_sheet(dests, False, lambda: "FAKE-PASSWORD", isatty=False, write=out.append) == 0
    assert "FAKE-PASSWORD" not in out[0] and "Brian Drive" in out[0] and "restic -r rclone:<remote>" in out[0]
    out.clear()
    assert recovery.print_sheet(dests, True, lambda: "FAKE-PASSWORD", isatty=False, write=out.append) == 2
    assert "FAKE-PASSWORD" not in out[0]
    out.clear()
    assert recovery.print_sheet(dests, True, lambda: "FAKE-PASSWORD", isatty=True, write=out.append) == 0
    assert "Backup password: FAKE-PASSWORD" in out[0]


def fake_bin(tmp_path):
    """A PATH with a 'security' that never finds anything, so the scripts can't touch a real Keychain."""
    b = tmp_path / "bin"
    b.mkdir()
    (b / "security").write_text("#!/bin/sh\nexit 44\n")
    (b / "security").chmod(0o755)
    return f"{b}:/usr/bin:/bin"


# M6 "done when": a migration kit dry run succeeds.
def test_migration_kit_dry_run(tmp_path):
    data = tmp_path / "data"
    (data / "export").mkdir(parents=True)
    (data / "tmp").mkdir()
    env = {"PATH": fake_bin(tmp_path), "HOME": str(tmp_path), "SCRIBE_BOT_DATA": str(data), "SCRIBE_BOT_REPO": str(REPO)}
    p = subprocess.run(["bash", str(REPO / "scripts" / "migration_kit.sh")], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "Dry run OK." in p.stdout and "- export:" in p.stdout and "- tmp:" in p.stdout and "(skipped)" in p.stdout
    real = subprocess.run(["bash", str(REPO / "scripts" / "migration_kit.sh"), "--go"], env=env, capture_output=True, text=True)
    assert real.returncode == 2


def test_backup_setup_script_only_prints_without_install(tmp_path):
    env = {"PATH": fake_bin(tmp_path), "HOME": str(tmp_path)}
    p = subprocess.run(["bash", str(REPO / "scripts" / "setup" / "m14_backup.sh")], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "would run:" in p.stdout and "install restic" in p.stdout
    assert "would create a random backup password" in p.stdout


def test_export_leaves_out_what_brian_removed(conn):
    from scribe_bot.backup import export
    t = "2026-10-19T10:00:00+00:00"
    cid = conn.execute("INSERT INTO conversation (title, created_at, updated_at) VALUES ('Synthetic', ?, ?)", (t, t)).lastrowid
    for title, status in (("Kept synthetic task", "open"), ("Removed synthetic task", "dropped")):
        conn.execute("INSERT INTO task (conversation_id, title, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (cid, title, status, t, t))
    for text, rated in (("Right synthetic decision", None), ("Wrong synthetic decision", "wrong")):
        conn.execute("INSERT INTO decision (conversation_id, text, rated, created_at) VALUES (?, ?, ?, ?)", (cid, text, rated, t))
    md = export.render(conn, conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone())
    assert "Kept synthetic task" in md and "Right synthetic decision" in md
    assert "Removed synthetic task" not in md and "Wrong synthetic decision" not in md


def _rec(conn, tmp_path, rid, status, outside=None):
    """A recording with made-up audio files in the data folder (and optionally one path outside it)."""
    files = []
    for sub in ("inbound", "vault", "archive"):
        f = tmp_path / sub / f"r{rid}.m4a"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"fake audio")
        files.append(f)
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, vault_path, archive_path) "
                 "VALUES (?, 'recorder', ?, ?, ?, ?, ?, ?)",
                 (rid, str(outside or files[0]), f"sha{rid}", T, status, str(files[1]), str(files[2])))
    return files


def test_trash_purge_deletes_audio_too(conn, tmp_path):
    """Brian, 2026-11-21: 30 days in Trash, then the audio goes too; Inbox Skip 30+ days ago the same; nothing else."""
    cfg = Settings(tmp_path)
    now = datetime(2026, 11, 30, tzinfo=timezone.utc)
    old_at, new_at = (now - timedelta(days=31)).isoformat(timespec="seconds"), (now - timedelta(days=5)).isoformat(timespec="seconds")
    gone = _rec(conn, tmp_path, 1, "done")
    old = conversation(conn, "Old", deleted_at=old_at)
    conn.execute("UPDATE conversation SET recording_id = 1 WHERE id = ?", (old,))
    keep = _rec(conn, tmp_path, 2, "done")
    recent = conversation(conn, "Recent", deleted_at=new_at)
    conn.execute("UPDATE conversation SET recording_id = 2 WHERE id = ?", (recent,))
    skipped = _rec(conn, tmp_path, 3, "declined")
    conn.execute("INSERT INTO review_item (kind, ref_id, status, resolved_at, created_at) VALUES ('approval', 3, 'dismissed', ?, ?)", (old_at, T))
    skipped_new = _rec(conn, tmp_path, 4, "declined")
    conn.execute("INSERT INTO review_item (kind, ref_id, status, resolved_at, created_at) VALUES ('approval', 4, 'dismissed', ?, ?)", (new_at, T))
    waiting = _rec(conn, tmp_path, 5, "awaiting_approval")
    elsewhere = tmp_path.parent / f"{tmp_path.name}-outside.m4a"
    elsewhere.write_bytes(b"not ours")
    _rec(conn, tmp_path, 6, "declined", outside=elsewhere)
    conn.execute("INSERT INTO review_item (kind, ref_id, status, resolved_at, created_at) VALUES ('approval', 6, 'dismissed', ?, ?)", (old_at, T))
    assert trash.purge(conn, now, cfg=cfg) == 1
    assert not any(f.exists() for f in gone + skipped)
    assert all(f.exists() for f in keep + skipped_new + waiting)
    assert elsewhere.exists()   # never outside the data folder
    marked = {r[0] for r in conn.execute("SELECT id FROM recording WHERE audio_deleted_at IS NOT NULL")}
    assert marked == {1, 3, 6}
    assert conn.execute("SELECT count(*) FROM recording").fetchone()[0] == 6   # rows stay: never imported again
    assert trash.purge(conn, now, cfg=cfg) == 0   # a second night changes nothing


def test_trash_purge_without_cfg_keeps_audio(conn, tmp_path):
    now = datetime(2026, 11, 30, tzinfo=timezone.utc)
    files = _rec(conn, tmp_path, 1, "done")
    cid = conversation(conn, "Old", deleted_at=(now - timedelta(days=31)).isoformat(timespec="seconds"))
    conn.execute("UPDATE conversation SET recording_id = 1 WHERE id = ?", (cid,))
    assert trash.purge(conn, now) == 1
    assert all(f.exists() for f in files)


def test_purged_audio_goes_on_the_device_cleanup_list(conn, tmp_path):
    """Brian, 2026-11-21: after the audio is deleted, ask to delete it from the Pixel / M4 too; one note per night."""
    from scribe_bot.core import cleanup_devices
    cfg = Settings(tmp_path)
    now = datetime(2026, 11, 30, tzinfo=timezone.utc)
    old_at = (now - timedelta(days=31)).isoformat(timespec="seconds")
    _rec(conn, tmp_path, 1, "done")
    cid = conversation(conn, "Old", deleted_at=old_at)
    conn.execute("UPDATE conversation SET recording_id = 1 WHERE id = ?", (cid,))
    _rec(conn, tmp_path, 3, "declined")
    conn.execute("UPDATE recording SET source = 'upload', title = '20261025 191200' WHERE id = 3")
    conn.execute("INSERT INTO review_item (kind, ref_id, status, resolved_at, created_at) VALUES ('approval', 3, 'dismissed', ?, ?)", (old_at, T))
    trash.purge(conn, now, cfg=cfg)
    rows = {r["recording_id"]: r["device"] for r in conn.execute("SELECT * FROM device_cleanup WHERE status = 'ask'")}
    assert rows == {1: "pixel", 3: "m4"}
    notes = [r[0] for r in conn.execute("SELECT title FROM notification WHERE link = '/cleanup'")]
    assert len(notes) == 1 and "2 recordings" in notes[0] and "Pixel (Recorder)" in notes[0] and "M4 (Voice Memos)" in notes[0]
    cleanup_devices.set_status(conn, None, "todo", only="ask")
    assert len(cleanup_devices.checklist(conn)) == 2
    trash.purge(conn, now, cfg=cfg)   # nothing new: no second note
    assert conn.execute("SELECT count(*) FROM notification WHERE link = '/cleanup'").fetchone()[0] == 1

"""Bug-type sweep (Brian's PREFERENCES §5, 2026-11-24). Synthetic data only."""
from test_web_pages import env, one  # noqa: F401  (env is a fixture)

from scribe_bot.core import db


def transcript_only(conn, cid=1, recording_done=True):
    """Talk 1 as a finished Transcript-only talk: no AI notes, no area, status 'transcribed' for good."""
    conn.execute("UPDATE conversation SET summary = NULL, area = NULL, area_confidence = NULL, status = 'transcribed', "
                 "opened_at = NULL WHERE id = ?", (cid,))
    conn.execute("UPDATE recording SET status = ? WHERE id = 1", ("done" if recording_done else "processing",))


def test_finished_transcript_only_talk_counts_like_done(env):
    """'transcribed' is finished for a Transcript-only talk: Not sorted, Today's recent list, the New badge."""
    client, conn, _ = env
    transcript_only(conn)
    assert "Weekly sync with Alex" in client.get("/conversations?unsorted=1").text
    talks = client.get("/conversations", cookies={"area": "work"}).text
    assert "Weekly sync with Alex" not in talks          # not filed: in Not sorted, not under every area
    assert "Weekly sync with Alex" in client.get("/").text
    assert "badge-new" in client.get("/conversations").text
    client.get("/conversations/1")
    assert one(conn, "SELECT opened_at FROM conversation WHERE id = 1") is not None
    assert "badge-new" not in client.get("/conversations").text


def test_transcribed_while_ai_still_runs_is_not_finished(env):
    client, conn, _ = env
    transcript_only(conn, recording_done=False)
    assert "Weekly sync with Alex" not in client.get("/conversations?unsorted=1").text


def test_transcript_only_talk_never_says_summary_on_its_way(env):
    client, conn, _ = env
    transcript_only(conn)
    conn.execute("UPDATE task SET status = 'open' WHERE conversation_id = 1")   # items, so the full page shows
    assert "Summary on its way" not in client.get("/conversations/1").text


def test_summarize_from_the_list_shows_working_and_a_note(env):
    """Swipe / right-click / select › Summarize: the row says Working… and a note says what's happening."""
    client, conn, _ = env
    transcript_only(conn)
    r = client.post("/talks-bulk", data={"ids": "1", "what": "ai", "confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/conversations?note=summarizing"
    page = client.get(r.headers["location"]).text
    assert "Summarizing." in page and "shows Summarizing until" in page
    row = page.split('data-id="1"')[1].split("</div>")[0]
    assert "Summarize" not in row                         # no second swipe to pay twice while it runs
    assert "Each talk shows Working" not in client.get("/conversations?note=<script>").text   # only known notes


def test_transcribe_from_the_inbox_says_it_started(env, tmp_path):
    client, conn, _ = env
    orig = tmp_path / "Oct 3 at 10_61.m4a"; orig.write_bytes(b"synthetic older audio")
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                 "VALUES (61, 'recorder', ?, 'old61', 600, ?, 'backlog', ?)", (str(orig), db.now(), orig.stem))
    r = client.post("/inbox/bulk", data={"ids": "61", "what": "local"}, headers={"referer": "/inbox?x=1"},
                    follow_redirects=False)
    assert r.headers["location"] == "/inbox?x=1&note=transcribing"
    assert "under Talks › Processing" in client.get(r.headers["location"]).text


def test_rail_and_icons(env):
    client, _, _ = env
    js = client.get("/static/app.js").text
    assert 'return "↑ Top";' in js                                          # always Top (Brian, 2026-11-25)
    ui = client.get("/static/ui.js").text
    assert "PICK_ROW" in ui                                                # Shift-click on the whole row picks a range
    page = client.get("/").text
    assert '<symbol id="i-bell" viewBox="0 1 24 24">' in page               # drawn in the middle of its circle
    assert ".back-link, .navlist a" in page and '"eagerness": "moderate"' not in page   # prefetch on press only (T7)
    assert "history.back()" in ui and "mouse-away" in ui                   # back to the page you came from; rails (N1)
    assert "icon-bars-192.png" in page and "/static/icon.svg?v=" in page   # sound-bar logo (H1), fresh after a deploy


def test_today_arrows_are_all_gray(env):
    """Brian (2026-11-23): 'Process N recordings' had the only blue › on Today. All arrows are gray."""
    client, _, _ = env
    assert 'class="go pri"' not in client.get("/").text


def test_more_drop_down_on_phone_and_less_folds_on_press(env):
    """C10: More/Less opens and closes in the phone drawer too. C7: 'Less' folds on the press itself (no jump)."""
    client, _, _ = env
    page = client.get("/").text
    assert 'class="side-more drawer-more"' in page and "drawer-sec" not in page
    ui = client.get("/static/ui.js").text
    assert "foldNow(box)" in ui and 'document.addEventListener("pointerdown"' in ui
    assert 'g = { kind: "nav"' not in ui                                    # no swipe-to-go-back
    assert 'class="sb-adv"' in client.get("/search?q=sync").text            # Advanced search beside the box on phones


def test_tasks_pick_several_done_not_needed_delete_and_undo(env):
    """N4: Tasks › pick several › Done / Not needed / Delete; one Undo brings them all back."""
    client, conn, _ = env
    page = client.get("/tasks").text
    assert 'data-pk-bar="task-list"' in page and 'id="task-list" data-picker' in page and "data-pk-cb" in page
    ids = [r[0] for r in conn.execute("SELECT id FROM task WHERE status = 'open' ORDER BY id LIMIT 2")]
    assert len(ids) == 2
    r = client.post("/tasks-bulk/done", data={"ids": ",".join(map(str, ids))}, follow_redirects=False)
    assert r.status_code == 303
    assert one(conn, f"SELECT count(*) FROM task WHERE id IN ({ids[0]}, {ids[1]}) AND status = 'done' AND closed_as IS NULL") == 2
    uid = one(conn, "SELECT max(id) FROM undo_action")
    client.post(f"/undo/{uid}", follow_redirects=False)
    assert one(conn, f"SELECT count(*) FROM task WHERE id IN ({ids[0]}, {ids[1]}) AND status = 'open'") == 2
    client.post("/tasks-bulk/not-needed", data={"ids": str(ids[0])}, follow_redirects=False)
    assert one(conn, "SELECT closed_as FROM task WHERE id = ?", ids[0]) == "not_needed"
    client.post("/tasks-bulk/delete", data={"ids": str(ids[1])}, follow_redirects=False)
    assert one(conn, "SELECT status FROM task WHERE id = ?", ids[1]) == "dropped"
    r = client.post("/tasks-bulk/explode", data={"ids": str(ids[1])}, follow_redirects=False)
    assert r.status_code == 303                                             # unknown action changes nothing


def test_trash_pick_several_restore_and_delete_forever(env):
    """N4: Trash › pick several › Restore or Delete forever (talks only; skipped recordings leave on their own)."""
    client, conn, _ = env
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = 1", (db.now(),))
    page = client.get("/conversations?trash=1").text
    assert 'data-pk-bar="trash-list"' in page and 'data-pick-id="c1"' in page
    r = client.post("/trash-bulk/restore", data={"ids": "c1"}, follow_redirects=False)
    assert r.headers["location"] == "/conversations?trash=1"
    assert one(conn, "SELECT deleted_at FROM conversation WHERE id = 1") is None
    client.post("/trash-bulk/forever", data={"ids": "c1"}, follow_redirects=False)   # not in Trash: left alone
    assert one(conn, "SELECT count(*) FROM conversation WHERE id = 1") == 1
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = 1", (db.now(),))
    client.post("/trash-bulk/forever", data={"ids": "c1,x9,r"}, follow_redirects=False)
    assert 'data-pick-id="c1"' not in client.get("/conversations?trash=1").text      # gone from Trash at once
    assert client.get("/conversations/1").status_code == 404
    conn.execute("UPDATE conversation SET purge_at = '2020-01-01T00:00:00+00:00' WHERE id = 1")   # its Undo time is over
    client.get("/conversations?trash=1")
    assert one(conn, "SELECT count(*) FROM conversation WHERE id = 1") == 0
    assert one(conn, "SELECT count(*) FROM task WHERE conversation_id = 1") == 0


def test_right_click_beyond_list_rows(env):
    """N6 / C4: lines, speakers, tags, pills, editable text, task details, links and ⋯ menus all have a right-click menu."""
    client, _, _ = env
    ui = client.get("/static/ui.js").text
    for bit in ("function other(e)", "Copy link to this line", "Who said this…", "Fix words", "Talks with ", "Who is it…",
                '"Edit " + lab', "Open in new tab", "function mirror(panel)", "window.ScribeToast = note"):
        assert bit in ui, bit
    assert "select.pill-sel" in ui                                        # Area / Type pills list their choices


def test_one_search_row_above_every_list(env):
    """N9 / A3.5: a search box first, then Filter and Sort pills, on Talks, Inbox, Tasks and People; % is literal."""
    client, conn, _ = env
    talks = client.get("/conversations?q=weekly").text
    assert 'id="talks-q"' in talks and 'class="fx-row tb-a lbar"' in talks and "Weekly sync with Alex" in talks
    assert "Weekly sync with Alex" not in client.get("/conversations?q=zzzz").text
    assert "Weekly sync with Alex" not in client.get("/conversations?q=%25%25").text   # % means a percent sign, not "anything"
    assert "Weekly sync with Alex" in client.get("/conversations?q=zephyr").text        # the summary counts too
    tasks = client.get("/tasks?q=deck").text
    assert 'id="tasks-q"' in tasks and "Review the launch deck" in tasks
    assert "Review the launch deck" not in client.get("/tasks?q=zzzz").text
    assert 'id="people-q"' in client.get("/people").text and "lbar" in client.get("/people").text
    assert client.get("/inbox?q=nothing").status_code == 200


def test_add_recording_page_has_a_computer_layout(env):
    """N10: two columns on a computer (drop zone + Record | note + Recent); picked files upload at once."""
    client, _, _ = env
    page = client.get("/capture").text
    assert "<h1>Add recording</h1>" in page and 'class="cap-grid"' in page and 'class="cap-zone"' in page
    assert 'action="/capture/upload"' in page and "this.form.requestSubmit()" in page and 'id="record"' in page
    assert page.index("cap-main") < page.index("cap-side")


def test_page_version_covers_every_static_file(env):
    """A new logo alone must change ?v= too (2026-11-25: the version only covered the CSS and JS)."""
    import hashlib
    from scribe_bot.web import app as web_app
    client, _, _ = env
    static = web_app.WEB / "static"
    want = hashlib.sha256(b"".join(f.name.encode() + f.read_bytes() for f in sorted(static.iterdir()) if f.is_file())).hexdigest()[:10]
    r = client.get("/")
    assert f"/static/icon.svg?v={want}" in r.text and f"/static/app.js?v={want}" in r.text


def test_line_link_block_has_its_own_open_transcript():
    """B16 (2026-11-25): /conversations/59#L6 never loaded the transcript. The "#L" handler called openTranscript(),
    which lived in another script block, so it stopped with a ReferenceError. Each block that calls it defines it."""
    import re
    from pathlib import Path
    js = Path("scribe_bot/web/static/app.js").read_text()
    blocks = re.split(r"\n\}\)\(\);\n", js)
    for b in blocks:
        if re.search(r"(?<!function )openTranscript\(\)", b):
            assert "function openTranscript()" in b, b[:120]
    lazy = next(b for b in blocks if 'htmx.trigger(b, "load-tx")' in b)
    assert "DOMContentLoaded" in lazy and "!ready" in lazy   # nothing is asked of htmx before it has started

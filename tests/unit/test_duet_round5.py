"""Claude Duet round 5 (Brian, 2026-11-25): code-level deep check. Each bug found gets a test here (synthetic only)."""
from html import unescape
from pathlib import Path

from test_web_pages import env, one  # noqa: F401
from test_web_voice_memos import memo_m4a, waiting

STATIC = Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static"
UI, APP, CSS = ((STATIC / f).read_text() for f in ("ui.js", "app.js", "app.css"))
POST = {"sec-fetch-site": "same-origin"}
PAGE = {"accept": "text/html,application/xhtml+xml"}
HUGE = "99999999999999999999"   # too big for SQLite's 64-bit integers


def test_r5_01_huge_ids_are_not_found_never_a_crash(env):
    client, _, _ = env
    client_safe = client.__class__(client.app, raise_server_exceptions=False)
    for path in (f"/conversations/{HUGE}", f"/people/{HUGE}", f"/audio/speaker/{HUGE}", f"/segments/{HUGE}/edit"):
        assert client_safe.get(path).status_code == 404, path
        assert client_safe.get(path, headers=PAGE).status_code == 404, path
    for path in (f"/tasks/{HUGE}/done", f"/undo/{HUGE}", f"/speakers/{HUGE}/name", f"/conversations/1/tags/{HUGE}/remove"):
        r = client_safe.post(path, headers=POST, data={"person_id": "2"})
        assert r.status_code == 404 and r.json() == {"detail": "Not found"}, path


def test_r5_02_link_to_something_deleted_gets_a_page_not_code(env):
    client, _, _ = env
    r = client.get("/conversations/999999", headers=PAGE)
    assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    page = unescape(r.text)
    assert '{"detail"' not in page and "That talk isn't here any more" in page and "Go to Today" in page
    assert 'class="tabbar' in r.text or 'class="navlist' in r.text           # the app frame, so the menus still work
    r = client.get("/no-such-page", headers=PAGE)
    assert r.status_code == 404 and "This page doesn't exist" in unescape(r.text)
    r = client.get("/conversations/abc", headers=PAGE)                        # not a number: no such page, not "422"
    assert r.status_code == 404 and "This page doesn't exist" in unescape(r.text)
    # the page's own code (htmx) still gets the short message it shows in its note
    r = client.get("/conversations/999999", headers={"hx-request": "true", **PAGE})
    assert r.json()["detail"].startswith("That talk isn't here")
    assert client.get("/people/999999").json()["detail"] == "That person isn't here any more (merged or deleted)"


def test_r5_03_task_edit_after_the_person_was_merged_elsewhere(env):
    client, conn, _ = env
    r = client.post("/tasks/1/edit", headers=POST, data={"title": "Review the launch deck", "owner": "777"})
    assert r.status_code == 409 and "merged or removed on another device" in r.json()["detail"]
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 1") == 1     # unchanged
    # any other clash the code didn't foresee says so instead of "Internal Server Error"
    from scribe_bot.web.app import create_app
    assert "sqlite3.IntegrityError" in Path(create_app.__code__.co_filename).read_text()


def test_r5_04_duplicate_check_survives_a_bad_request(env):
    client, _, _ = env
    for body in (b"", b"not json", b"[1, 2]", b'{"hashes": "abc"}', b'{"hashes": [1, null]}'):
        r = client.post("/capture/known", headers={**POST, "content-type": "application/json"}, content=body)
        assert r.status_code == 200 and r.json() == {"known": {}}, body


def test_r5_05_voice_memo_with_emoji_name_downloads(env):
    client, conn, cfg = env
    up = cfg.data_dir / "uploads"; up.mkdir(parents=True, exist_ok=True)
    memo_m4a(up / "会议 🎙.m4a", [("Garden ", 0.0), ("plans.", 0.5)])
    waiting(conn, up, rid=41, name="会议 🎙")
    r = client.get("/inbox/41/transcript.txt")
    assert r.status_code == 200 and r.text.startswith("Garden")
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''%E4%BC%9A%E8%AE%AE%20%F0%9F%8E%99.txt"


def test_r5_06_person_picker_escapes_quotes_and_letters():
    start = APP.index("/* Person picker")
    picker = APP[start:APP.index("})();", start)]
    assert '\'"\': "&quot;"' in picker                                      # attribute-safe escaping
    assert "L = esc(L); h += '<span class=\"pp-l\"" in picker
    assert "letters.map(function (L) { L = esc(L);" in picker


def test_r5_07_other_sites_cannot_frame_the_app(env):
    client, _, _ = env
    for path in ("/", "/conversations/1", "/static/app.js"):
        h = client.get(path).headers
        assert h["x-frame-options"] == "SAMEORIGIN" and h["content-security-policy"] == "frame-ancestors 'self'", path
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "same-origin"


def test_r5_08_undo_still_works_after_the_person_was_merged(env):
    """Phone: mark a task done (Undo bar shows). Computer: merge that task's person away. Phone: Undo. It crashed;
    now the task reopens and keeps the person it has now."""
    client, conn, _ = env
    assert client.post("/tasks/2/done", headers=POST).status_code in (200, 303)
    uid = one(conn, "SELECT max(id) FROM undo_action")
    conn.execute("INSERT INTO person (id, name, created_at) VALUES (9, 'Alexa Example', '2026-01-01')")
    conn.execute("UPDATE task SET owner_person_id = 9 WHERE owner_person_id = 2")   # what a merge into 9 leaves behind
    conn.execute("UPDATE speaker SET person_id = 9 WHERE person_id = 2")
    conn.execute("DELETE FROM person_fact WHERE person_id = 2")
    conn.execute("DELETE FROM person WHERE id = 2")
    r = client.post(f"/undo/{uid}", headers=POST)
    assert r.status_code in (200, 303)
    assert one(conn, "SELECT status FROM task WHERE id = 2") == "open"
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 2") == 9


def test_r5_09_pdf_keeps_dashes_and_underscores(env):
    """fpdf's markdown read "--" as underline and "__" as italics and dropped them from the text."""
    import re
    import zlib
    client, conn, _ = env
    conn.execute("UPDATE segment SET text = 'We need -- I think -- to ship __init__ and a ** star' WHERE id = 1")
    pdf = client.get("/conversations/1/export.pdf").content
    text = b"".join(zlib.decompress(m) for m in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.S)
                    if m[:2] in (b"x\x9c", b"x\xda"))
    assert b"We need -- I think -- to ship __init__ and a ** star" in text.replace(b") Tj /F1 10.00 Tf (", b"")
    assert b"(Alex Example:)" in text or b"Alex Example:" in text                     # the name is still bold


def test_r5_10_a_form_goes_once_on_a_double_tap():
    """Two quick taps on a plain form's button sent it twice (two tasks, a line split twice). jsdom checked: three
    submits in a row send one; after coming back to the page (pageshow) it can be sent again."""
    assert 'window.addEventListener("submit", function (e) {' in APP
    guard = APP[APP.index("R5-10"):APP.index("Fallback for full-page form posts")]
    assert "e.defaultPrevented" in guard and "Date.now() - t < 4000" in guard and "f.target) return;" in guard
    assert 'addEventListener("pageshow"' in guard


def test_r5_11_no_error_codes_on_screen():
    """'That didn't work (error 500)' broke the rule: a plain sentence that says what to do, never a code."""
    assert "(error \" +" not in APP and "(error \" +" not in UI
    assert "window.sbFail = function (status)" in APP and "window.sbFail(d.xhr.status)" in APP


def test_r5_12_no_browser_alert_boxes():
    """Two failures (bulk merge on Talks and Inbox) showed the browser's own alert box with raw text."""
    import re
    for js in (APP, UI):
        assert not re.search(r"(?<![\w.])alert\(", js)
    assert "window.sbNote = netNote;" in APP
    assert "r.json().catch(function () { return {}; })" in APP                # a non-JSON error page no longer breaks it


def test_r5_13_failed_sends_never_leave_a_dead_button():
    """Voice note upload stuck on 'Uploading…' on a failure; the mic being refused did nothing; the Select bar's
    post (Delete, Merge…) stayed disabled after a failure; the person list failed silently."""
    assert 'rec.textContent = "Didn\'t save · Tap to try again"' in APP and "rec._retry()" in APP
    assert "The microphone isn't allowed." in APP
    assert "b.disabled = false; window.sbNote(" in APP
    assert "Couldn't load the list of people." in APP


def test_r5_14_notification_links_stay_in_the_app(env):
    client, conn, _ = env
    for i, link in enumerate(("//evil.example/x", "/\\evil.example", "https://evil.example", "/conversations/1")):
        conn.execute("INSERT INTO notification (id, channel, title, link, created_at) VALUES (?, 'bot', 't', ?, '2026-11-25')",
                     (900 + i, link))
        r = client.get(f"/notifications/{900 + i}/open", follow_redirects=False)
        assert r.headers["location"] == (link if link == "/conversations/1" else "/notifications"), link


def test_r5_15_no_player_when_the_audio_is_gone(env):
    """The player and the ▶ timestamps showed for a talk whose audio file was gone; every tap did nothing."""
    client, conn, cfg = env
    page = client.get("/conversations/1?part=lines").text
    assert 'id="player"' in page and "data-seek=" in page
    (cfg.data_dir / "vault" / "sample.m4a").unlink()
    page = client.get("/conversations/1?part=lines").text
    assert 'id="player"' not in page and "data-seek=" not in page
    assert client.get("/audio/conversation/1").status_code == 404


def test_r5_15b_voice_button_only_for_a_clip_that_is_there(env):
    client, conn, cfg = env
    assert 'data-voice="/audio/speaker/3"' in client.get("/conversations/1").text
    (cfg.data_dir / "vault" / "clip.wav").unlink()
    assert 'data-voice="/audio/speaker/3"' not in client.get("/conversations/1").text
    assert "Couldn't play this voice sample." in APP


def test_r5_16_export_matches_what_the_app_shows(env):
    """The .md file said '20:24 (UTC)', 'Area: personal' and ticked Not needed tasks as plain done."""
    from scribe_bot.web.app import local_datetime
    client, conn, _ = env
    c = conn.execute("SELECT id, coalesce(started_at, created_at) AS t FROM conversation WHERE deleted_at IS NULL LIMIT 1").fetchone()
    conn.execute("UPDATE conversation SET area = 'personal' WHERE id = ?", (c["id"],))
    conn.execute("INSERT INTO task (conversation_id, title, owner_person_id, status, closed_as, created_at, updated_at) "
                 "VALUES (?, 'Skip the extra call', 1, 'done', 'not_needed', '2026-11-20', '2026-11-20')", (c["id"],))
    conn.commit()
    md = client.get(f"/conversations/{c['id']}/export.md").text
    assert "(UTC)" not in md and "Area: Home" in md
    shown = local_datetime(c["t"])                                     # 'Tue Nov 25, 3:24 pm' on the page
    when = next(x for x in md.splitlines() if x.startswith("- When: "))
    assert shown.split(", ")[-1] in when and shown.split(",")[0] in when  # same day and clock as the page
    assert "Skip the extra call (Me, not needed)" in md


def test_r5_17_more_and_settings_have_the_back_link_at_the_top(env):
    """'A back link at the top of every page, same place and style' (Brian, 2026-11-25): More and the Settings list had none,
    and on a computer a Settings group had none (its '‹ Settings' sat inside the pane and is hidden there)."""
    client, _, _ = env
    for path in ("/more", "/settings", "/settings/appearance"):
        page = client.get(path).text
        main = page[page.index('id="content"'):]
        assert main.split(">", 1)[1].lstrip().startswith('<a class="back-link'), path   # first thing on the page
    page = client.get("/settings/appearance").text
    assert 'class="back-link settings-back" href="/settings">‹ Settings' in page          # phone: up to the list
    assert 'class="back-link settings-back-wide" href="/">‹ Today' in page               # computer: groups are on the left
    assert page.count("‹ Settings") == 1
    assert ".settings-back { display: none } .settings-back-wide { display: inline-flex }" in CSS


def test_r5_18_undo_notes_are_short_with_no_item_names():
    """Brian's rule: status notes stay short ("Moved to Trash", no item names). Task and person notes quoted the whole
    title, which wrapped to two lines on a phone and got cut off."""
    import re
    src = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "routes.py").read_text()
    labels = re.findall(r'undo\.save\(conn, (f?"[^"]*")', src)
    assert labels
    for lab in labels:
        assert "title" not in lab and "“" not in lab and "p['name']" not in lab, lab


def test_r5_19_badge_counts_on_every_page_use_an_index(env):
    """Each page counts the Inbox's older recordings and the talks in Trash; both read the whole table before."""
    _, conn, _ = env
    for q in ("SELECT count(*) FROM recording WHERE status = 'backlog'",
              "SELECT count(*) FROM conversation WHERE deleted_at IS NOT NULL"):
        plan = " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + q))
        assert "USING" in plan and "INDEX" in plan, (q, plan)


def test_r5_20_no_code_left_from_in_place_page_changes(env):
    """hx-boost (pages swapped in place) was removed on 2026-11-22; its handlers and header stayed behind unused."""
    client, _, _ = env
    assert "boosted" not in APP and "X-Asset-V" not in APP and "navWatch" not in APP
    assert "x-asset-v" not in client.get("/").headers
    assert 'hx-boost="true"' not in client.get("/").text


def test_r5_21_undo_keeps_what_was_changed_since(env):
    """Phone marks a task done (its Undo bar shows); the computer renames it; the phone taps Undo. The rename was lost."""
    client, conn, _ = env
    client.post("/tasks/1/done", headers=POST)
    uid = conn.execute("SELECT max(id) FROM undo_action").fetchone()[0]
    client.post("/tasks/1/edit", headers=POST, data={"title": "Renamed on the computer"})
    assert client.post(f"/undo/{uid}", headers=POST, follow_redirects=False).status_code == 303
    t = conn.execute("SELECT status, title FROM task WHERE id = 1").fetchone()
    assert tuple(t) == ("open", "Renamed on the computer")
    # a plain Undo (nothing since) still puts everything back
    before = tuple(conn.execute("SELECT status, title, due FROM task WHERE id = 2").fetchone())
    client.post("/tasks/2/done", headers=POST)
    uid = conn.execute("SELECT max(id) FROM undo_action").fetchone()[0]
    client.post(f"/undo/{uid}", headers=POST)
    assert tuple(conn.execute("SELECT status, title, due FROM task WHERE id = 2").fetchone()) == before


def test_r5_22_playback_highlight_reads_the_lines_once():
    """While audio played, every 0.1 s looked up every line and parsed its times again (thousands on a long talk).
    The list is kept and dropped when the page changes (checked in jsdom against the old logic after edits)."""
    body = APP[APP.index("function highlight(a)"):APP.index("function highlight(a)") + 900]
    assert "lines()" not in body and "parseFloat" not in body and "timed()" in body
    assert 'attributeFilter: ["data-start", "data-end"]' in APP and "timedCache = null" in APP


def test_r5_23_menus_follow_the_pane_that_scrolls():
    """On a computer the list, the reading pane and settings scroll on their own. A drop-down's menu only closed when the
    whole page scrolled, so it floated in place while its box scrolled away; the tag list was placed once and never moved
    (checked in jsdom: scrolling a pane closes the menu, scrolling the menu itself doesn't, the tag list follows its box)."""
    sel = UI[UI.index("R4-17"):]
    assert '{ capture: true, passive: true }' in sel[:sel.index("htmx:beforeSwap")]
    assert "menu.contains(e.target)" in sel
    tags = UI[UI.index("R4-15 + R4-16"):UI.index("R4-17")]
    assert "function place()" in tags and 'addEventListener("resize", place)' in tags and "list.contains(e.target)" in tags


def test_r5_24_inbox_summarize_shows_the_cost_first(env, tmp_path):
    """Inbox › swipe › Summarize and Inbox › Select › Summarize sent recordings to the paid AI at once, with no cost shown
    (CLAUDE.md rule 7: a cost estimate before any cloud run). Now both open the confirm page with the total, like Talks."""
    import json
    client, conn, _ = env
    folder = tmp_path / "Recordings"; folder.mkdir()
    waiting(conn, folder, rid=20)
    waiting(conn, folder, rid=21, name="Private chat _ Oct 14 at 11_40")
    conn.execute("UPDATE review_item SET payload = json_set(payload, '$.private', json('true'), '$.ai_cost', 0.0) WHERE ref_id = 21")
    orig = tmp_path / "Oct 3 at 10_15.m4a"; orig.write_bytes(b"synthetic older audio")
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                 "VALUES (62, 'recorder', ?, 'old62', 600, '2026-10-03T10:15:00+00:00', 'backlog', 'Oct 3 at 10_15')", (str(orig),))
    conn.commit()
    jobs = "SELECT count(*) FROM job WHERE kind = 'recording'"
    before = one(conn, jobs)
    # the select bar (and anything else without the cost on its button) is sent to the confirm step, nothing runs
    r = client.post("/inbox/bulk", headers=POST, data={"ids": "20,21,62", "what": "ai"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inbox-bulk/ai?ids=20,21,62" and one(conn, jobs) == before
    page = unescape(client.get("/inbox-bulk/ai?ids=20,21,62").text)
    assert "Summarize 3 recordings?" in page and "1 Private recording stays on the M14" in page and "$0.0" in page
    assert 'name="confirm" value="1"' in page and 'value="20,21,62"' in page
    # the swipe: Summarize opens the cost (GET, row stays), never the form that spends; a Private one has no Summarize
    inbox = client.get("/inbox").text
    def row(rid):
        return inbox.split(f'id="rec-{rid}"')[1].split("</details>")[0]
    sw = json.loads(unescape(row(20).split("data-sw='")[1].split("'")[0]))
    assert [a["f"] for a in sw["r"] if a["t"] == "Summarize"] == ["form.sw-sum"]
    assert 'action="/review/' in row(20).split('class="sw-sum"')[0].rsplit("<form", 1)[1] and "data-sw-keep" in row(20)
    assert "Summarize" not in json.loads(unescape(row(21).split("data-sw='")[1].split("'")[0]))["r"].__repr__()
    assert 'action="/inbox-bulk/ai" class="sw-sum" data-sw-keep' in row(62)
    # a button that shows its cost still runs in one tap
    assert client.post("/inbox/bulk", headers=POST, data={"ids": "62", "what": "ai", "confirm": "1"}, follow_redirects=False).status_code == 303
    assert one(conn, jobs) == before + 1
    assert 'location.href = "/inbox-bulk/ai?ids="' in APP


def test_r5_25_confirm_page_has_a_back_link_and_whole_tokens(env, tmp_path):
    """The cost page, opened as its own page (swipe or right-click › Summarize), had no back link at the top (every other
    page has one) and could say "~7,500.0 tokens"."""
    client, conn, _ = env
    folder = tmp_path / "Recordings"; folder.mkdir()
    waiting(conn, folder, rid=20)
    conn.commit()
    page = client.get("/inbox-bulk/ai?ids=20").text
    assert '<a class="back-link" href="/inbox">‹ Inbox</a>' in page and ".0 tokens" not in page and "tokens" in page
    assert '<a class="back-link" href="/conversations">‹ Talks</a>' in client.get("/talks-bulk/ai?ids=1").text
    rid = one(conn, "SELECT id FROM review_item WHERE ref_id = 20")
    assert '<a class="back-link" href="/review">‹ Review</a>' in client.get(f"/review/{rid}/confirm").text
    # inline (inside the page it was opened from) it has no back link of its own
    assert "back-link" not in client.get("/inbox-bulk/ai?ids=20", headers={"hx-request": "true"}).text


def test_r5_26_fold_labels_say_hide_never_less():
    """Brian (2026-11-25): fold/unfold labels say Hide / Show, never "Less". Three folds still said "See less" (the
    Inbox preview, a talk's speakers, a long date group on Talks)."""
    import re
    for src in (APP, UI):
        assert not re.search(r"""textContent = [^;]*["'](See |Show )?[Ll]ess["']""", src)
    assert APP.count('? "Hide" :') >= 3


def test_r5_27_drawn_icons_not_emoji_in_icon_slots(env):
    """Brian (2026-11-25): emoji used as icons look different from the drawn ones; use the app's drawn icons everywhere.
    The task and note sheets (📅 👤 ⏰ 💬 🏷️), Clean up (🧹), the Filter button (⚙) and Merging (⧉) still used emoji."""
    import re
    tpl = Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "templates"
    emoji = re.compile("[\U0001F300-\U0001FAFF⧉⚙⏰]")
    for f in ("macros.html", "_status_sheet.html", "today.html", "_review.html", "inbox.html", "people.html", "conversation.html"):
        code = re.sub(r"\{#.*?#\}", "", (tpl / f).read_text(), flags=re.S)
        assert not emoji.search(code), (f, emoji.findall(code))
    assert not emoji.search(re.sub(r"//.*|/\*.*?\*/", "", APP, flags=re.S))
    assert '<symbol id="i-tag"' in (tpl / "icons.html").read_text()
    client = env[0]
    assert 'use href="#i-calendar"' in client.get("/tasks").text

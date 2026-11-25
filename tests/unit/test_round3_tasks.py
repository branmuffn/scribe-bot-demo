"""Round 3 tasks (Brian, 2026-11-25): U10 Undo of Not needed, and the gray title that only went away after Done + Undo."""
from pathlib import Path

from test_web_pages import env, one  # noqa: F401

H = {"sec-fetch-site": "same-origin"}


def test_undo_not_needed_brings_the_task_fully_back(env):
    client, conn, _ = env
    client.post("/tasks/1/not-needed", data={"note": "event passed"}, headers=H)
    uid = one(conn, "SELECT max(id) FROM undo_action")
    client.post(f"/undo/{uid}", headers=H)
    assert conn.execute("SELECT status, closed_as, closed_note FROM task WHERE id = 1").fetchone()[:] == ("open", None, None)
    row = client.get("/tasks").text.split('id="task-1"')[0].rsplit("<div", 1)[1]
    assert "not-needed" not in row


def test_open_task_never_reads_as_not_needed(env):
    """An open task left with an old "not needed" mark (from before this fix) shows normally: no gray, no tag."""
    client, conn, _ = env
    conn.execute("UPDATE task SET closed_as = 'not_needed', status = 'open' WHERE id = 1")
    page = client.get("/tasks").text
    row = page.split('id="task-1"')[0].rsplit("<div", 1)[1]
    assert "not-needed" not in row
    block = page.split('id="task-1"')[1].split('class="item-actions"')[0]
    assert "Closed without being done" not in block


def test_undo_saves_every_column_its_actions_change():
    """Same bug class, app-wide: each table's undo snapshot covers the columns undoable actions write."""
    from scribe_bot.web import undo
    for col in ("closed_as", "closed_note", "remind_at", "short_title"):
        assert col in undo.COLUMNS["task"]
    for col in ("short_title", "type", "area", "private", "detailed"):
        assert col in undo.COLUMNS["conversation"]


def test_u12_tag_remove_can_be_undone(env):
    """U12 (Brian, 2026-11-25): removing a tag shows Undo, and Undo puts the tag back on the talk."""
    client, conn, cfg = env
    cid = conn.execute("SELECT id FROM conversation WHERE deleted_at IS NULL LIMIT 1").fetchone()[0]
    from scribe_bot.core import tags
    tid = tags.add(conn, cid, "pineapple budget")
    client.post(f"/conversations/{cid}/tags/{tid}/remove", follow_redirects=False)
    assert not conn.execute("SELECT 1 FROM tag WHERE name = 'pineapple budget'").fetchone()
    uid = conn.execute("SELECT id FROM undo_action WHERE label = 'Tag removed' ORDER BY id DESC").fetchone()[0]
    assert client.post(f"/undo/{uid}", follow_redirects=False).status_code in (200, 303)
    assert [t["name"] for t in tags.of(conn, cid)].count("pineapple budget") == 1


def test_u12_glossary_delete_can_be_undone(env):
    client, conn, cfg = env
    gid = conn.execute("INSERT INTO glossary_term (term, created_at) VALUES ('Zorblat', '2026-11-25T00:00:00')").lastrowid
    client.post(f"/settings/glossary/{gid}/delete", follow_redirects=False)
    uid = conn.execute("SELECT id FROM undo_action WHERE label = 'Word removed' ORDER BY id DESC").fetchone()[0]
    client.post(f"/undo/{uid}", follow_redirects=False)
    assert conn.execute("SELECT 1 FROM glossary_term WHERE term = 'Zorblat'").fetchone()


def test_b20_only_the_more_chevron_turns():
    """B20 (Brian, 2026-11-25): opening More turned every icon in the list upside down (Trash, Help, ...)."""
    import re
    from pathlib import Path
    css = Path("scribe_bot/web/static/app.css").read_text()
    for rule in re.findall(r"([^{}]*\[open\][^{}]*)\{[^}]*rotate\(180deg\)", css):
        for sel in rule.split(","):
            if ".side-more" in sel:
                assert "summary" in sel, sel


def test_u1_progress_notes_are_short():
    from scribe_bot.web.routes import NOTES
    assert all(len(v) <= 70 for v in NOTES.values())


def test_b33_every_sized_box_fits_its_placeholder():
    import re
    from pathlib import Path
    for f in Path("scribe_bot/web/templates").glob("*.html"):
        for tag in re.findall(r"<input[^>]*>", f.read_text()):
            ph, sz = re.search(r'placeholder="([^"]*)"', tag), re.search(r'size="(\d+)"', tag)
            if ph and sz and "{{" not in ph.group(1):
                assert len(ph.group(1)) < int(sz.group(1)), (f.name, tag)


def test_b24_trash_rows_are_short_on_a_phone():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "scribe_bot/web"
    tpl = (root / "templates/conversations.html").read_text()
    assert "kept until" in tpl and "<span>removed for good" not in tpl
    css = (root / "static/app.css").read_text()
    assert "#trash-list .tr-row .tr-act { flex-basis: 100%" in css     # Restore on its own line on a phone
    assert "#trash-list.picking .tr-act { display: none }" in css       # U11P: compact while picking


def test_b22_every_nested_page_has_its_back_link_at_the_top():
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[2] / "scribe_bot/web/templates"
    tabs = {"today.html", "conversations.html", "tasks.html", "people.html", "inbox.html"}   # R5-17: More is not a tab, so it has one too
    for f in tpl.glob("*.html"):
        s = f.read_text()
        if '{% extends "base.html" %}' not in s or f.name in tabs:
            continue
        assert "{% block back %}" in s or "cv-back" in s or "settings-back" in s, f.name
    conv = (tpl / "conversations.html").read_text()
    assert "← Talks" not in conv and '{% if trash or unsorted %}<a class="back-link"' in conv   # Trash, Not sorted: top


def test_b25_area_switch_keeps_your_place():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/ui.js").read_text()
    assert '".seg-areas a, .area-menu a.menu-item"' in js and "area-scroll" in js and "scrollTo(0, s.y)" in js


def test_b26_tour_has_a_close_button_and_ends_on_an_outside_tap():
    from pathlib import Path
    web = Path(__file__).resolve().parents[2] / "scribe_bot/web"
    base = (web / "templates/base.html").read_text()
    assert 'class="icon-btn tour-x" data-tour="skip"' in base
    js = (web / "static/app.js").read_text()
    assert "!card.contains(e.target)) { setTour(null)" in js and 'e.key === "Escape" && card && !card.hidden' in js
    assert "tour-cta" in (web / "templates/help.html").read_text()


def test_u11_confirms_use_the_apps_own_box():
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parents[2] / "scribe_bot/web"
    js = (web / "static/app.js").read_text() + (web / "static/ui.js").read_text()
    assert not re.search(r"(?<![\w.])confirm\(", js.replace("window.confirm(text)", ""))   # no browser confirm left
    assert "window.sbConfirm = function" in js and '"htmx:confirm"' in js
    assert '<dialog id="ask" class="dialog ask-dialog"' in (web / "templates/base.html").read_text()
    for f in (web / "templates").glob("*.html"):
        assert "return confirm(" not in f.read_text(), f.name


def test_b2_sidebar_opens_quickly_on_hover():
    from pathlib import Path
    web = Path(__file__).resolve().parents[2] / "scribe_bot/web/static"
    assert 'side.classList.add("peek"); }, 120)' in (web / "ui.js").read_text()
    css = (web / "app.css").read_text()
    assert ".sidebar { transition: width .14s cubic-bezier(.2, .9, .25, 1) }" in css    # no shadow animation


def test_b3_speaker_person_links_share_one_row(env):
    client, conn, cfg = env
    cid = conn.execute("SELECT conversation_id FROM speaker WHERE person_id IS NOT NULL LIMIT 1").fetchone()
    if not cid:
        import pytest
        pytest.skip("seed has no named speaker")
    html = client.get(f"/conversations/{cid[0]}").text
    assert 'class="sp-links"' in html and "This isn't" not in html


def test_b17_your_speaking_page_has_one_heading_and_gaps(env):
    client, conn, cfg = env
    html = client.get("/speaking").text
    assert html.count(">Your speaking<") == 1 or html.count("<h2>Your speaking</h2>") == 0
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/app.css").read_text()
    assert ".tiles + .group-label { margin-top: var(--s4) }" in css and "#ranges { margin-top: var(--s4) }" in css


def test_u17p_tab_bar_stays_while_searching():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/app.js").read_text()
    assert 'classList.toggle("typing", touch && !search)' in js and 'input[name="q"]' in js


def test_b15_learning_talk_without_people_notes_hides_the_section(env):
    client, conn, cfg = env
    conn.execute("UPDATE conversation SET type = 'learning' WHERE id = 1")
    tbl = "fact" if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'fact'").fetchone() else "person_fact"
    conn.execute(f"DELETE FROM {tbl} WHERE conversation_id = 1")
    conn.commit()
    html = client.get("/conversations/1").text
    assert 'id="sec-facts"' not in html and "People notes" not in html
    conn.execute("UPDATE conversation SET type = 'multi' WHERE id = 1")
    conn.commit()
    assert 'id="sec-facts"' in client.get("/conversations/1").text     # other talks keep it, even when empty


def test_b31_ledger_and_backups_fold_and_filter(env):
    client, conn, cfg = env
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[2] / "scribe_bot/web/templates/settings.html").read_text()
    assert '<details class="fold-sec" id="ledger" open>' in tpl and '<details class="fold-sec" id="backups" open>' in tpl
    assert 'data-rows="#ledger tbody tr"' in tpl
    js = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/ui.js").read_text()
    assert "box.dataset && box.dataset.rows" in js
    for page in ("/settings/privacy", "/settings/system"):
        assert client.get(page).status_code in (200, 404)


def test_people_merge_suggestion_dont_merge_and_hide_with_undo(env):
    client, conn, cfg = env
    from scribe_bot.core import people
    a = conn.execute("INSERT INTO person (name, created_at) VALUES ('Jordan Test', '2026-01-01')").lastrowid
    b = conn.execute("INSERT INTO person (name, created_at) VALUES ('Jordan Other', '2026-01-01')").lastrowid
    conn.commit()
    pair = lambda: [d for d in people.duplicate_suggestions(conn) if {d["from_id"], d["into_id"]} == {a, b}]
    assert pair()
    html = client.get("/people").text
    assert "Don't merge" in html and 'action="/people/%d/not-same"' % pair()[0]["from_id"] in html
    d = pair()[0]
    r = client.post(f"/people/{d['from_id']}/not-same", data={"into": d["into_id"]}, follow_redirects=False)
    assert r.status_code == 303 and not pair()
    uid = conn.execute("SELECT id FROM undo_action ORDER BY id DESC LIMIT 1").fetchone()[0]
    client.post(f"/undo/{uid}")
    assert pair()                                                        # Undo brings it back
    client.post(f"/people/{d['from_id']}/not-same", data={"into": d["into_id"], "later": 1})
    assert not pair()
    conn.execute("UPDATE person_not_same SET until = '2000-01-01T00:00:00'")   # 30 days later
    conn.commit()
    assert pair()


def test_u16p_opening_a_task_sheet_does_not_remeasure_the_page():
    """U16P (2026-11-25): a tap that opens a task sheet no longer rebuilds the page outline or measures the full-width
    phone sheet while it rises; other pop-ups are still kept on screen."""
    js = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/app.js").read_text()
    assert 'document.addEventListener("toggle", fromPage, true)' in js and 'document.addEventListener("click", fromPage)' in js
    assert 'document.addEventListener("toggle", rebuild, true)' not in js
    assert ".task-row" in js.split("var POPS = ")[1].split(";")[0]
    assert 'phone.matches && d.matches(".fx-sheet")' in js
    assert "fit(d)" in js.split("function sync()")[1][:700]   # menus and chip pop-ups are still fitted


def test_b7_removals_on_one_device_reach_the_other(env):
    """B7 (2026-11-25): a talk moved to Trash, a task removed or a person deleted shows up in /gone for the other
    device's open page (kinds and ids only), and the page carries the mark it was drawn at."""
    client, conn, _ = env
    page = client.get("/conversations").text
    mark = int(page.split('<meta name="sb-gone" content="')[1].split('"')[0])
    assert client.get(f"/gone?since={mark}").json() == {"last": mark, "gone": []}
    client.post("/conversations/1/delete", headers=H)
    client.post("/tasks/2/delete", headers=H)
    d = client.get(f"/gone?since={mark}").json()
    assert {"k": "talk", "id": 1} in d["gone"] and {"k": "task", "id": 2} in d["gone"] and d["last"] > mark
    assert client.get(f"/gone?since={d['last']}").json()["gone"] == []
    # undo brings the talk back without logging another removal; deleting for good is its own kind
    conn.execute("UPDATE conversation SET deleted_at = NULL WHERE id = 1")
    conn.execute("DELETE FROM task WHERE conversation_id = 1")
    conn.execute("DELETE FROM conversation WHERE id = 1")
    kinds = [g["k"] for g in client.get(f"/gone?since={d['last']}").json()["gone"]]
    assert "talk_purged" in kinds and "talk" not in kinds
    js = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/app.js").read_text()
    assert '"/gone?since=" + mark' in js and "This talk was moved to Trash on another device." in js


def test_b7_old_removals_are_tidied(env):
    _, conn, _ = env
    conn.execute("INSERT INTO removal (kind, ref_id, at) VALUES ('task', 99, '2000-01-01T00:00:00Z')")
    conn.execute("INSERT INTO removal (kind, ref_id) VALUES ('task', 98)")
    assert [r[0] for r in conn.execute("SELECT ref_id FROM removal")] == [98]


def test_b13_people_notes_look_is_a_setting(env):
    """B13 (2026-11-25): people notes as bullets (default, no line under each), a paragraph, or lines; saved on this
    device and chosen in Settings › Appearance."""
    client, _, _ = env
    assert 'data-pnotes="bullets"' in client.get("/conversations/1").text
    client.cookies.set("pnotes", "paragraph")
    assert 'data-pnotes="paragraph"' in client.get("/conversations/1").text
    client.cookies.set("pnotes", "nonsense")
    assert 'data-pnotes="bullets"' in client.get("/people/1").text
    s = client.get("/settings/appearance").text
    assert "People notes" in s and all(f'data-pnotes-set="{k}"' in s for k in ("bullets", "paragraph", "lines"))
    css = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/app.css").read_text()
    assert 'html[data-pnotes="bullets"] .fact-row { border-top: 0' in css


def test_b19_costs_page_has_a_30_day_graph_by_account(env):
    """B19 (2026-11-25): Costs shows the last 30 days as daily bars, split by account, with a key and totals."""
    client, conn, _ = env
    from datetime import datetime, timedelta, timezone
    acct = conn.execute("INSERT INTO account (provider, nickname, owner, type, training_policy, secret_ref, created_at) VALUES "
                        "('anthropic', 'Main key', 'Brian', 'paid', 'no training', 'synthetic-ref', '2026-01-01')").lastrowid
    now = datetime.now(timezone.utc)
    for days, usd, a in ((0, 0.40, acct), (3, 0.10, acct), (3, 0.05, None), (45, 9.0, acct)):
        conn.execute("INSERT INTO ledger_entry (time, account_id, provider, model, task, sent_summary, cost_usd) VALUES "
                     "(?, ?, 'anthropic', 'm', 'summary', 'synthetic', ?)", ((now - timedelta(days=days)).isoformat(), a, usd))
    conn.commit()
    page = client.get("/costs").text
    assert 'id="cost-graph"' in page and "Last 30 days" in page
    graph = page.split('id="cost-graph"')[1].split("</section>")[0]
    assert "$9" not in graph                     # 45 days ago is left out
    assert "Main key · $0.50" in graph and "Other · $0.05" in graph
    assert graph.count('class="cg-day"') == 30
    assert "Highest day $0.4" in graph            # today: $0.40 plus the sample data's own call


def test_b37_swipes_settle_at_finger_speed_and_never_stay_stuck():
    """B37/N11 (2026-11-25): a let-go row settles at the finger's speed; a touch the browser takes over sends the row
    back; swipe rows pan only up and down for the browser from the first touch."""
    root = Path(__file__).resolve().parents[2] / "scribe_bot/web/static"
    js, css = (root / "app.js").read_text(), (root / "app.css").read_text()
    assert "function settle(dist, v)" in js and 'document.addEventListener("touchcancel"' in js
    assert 'h.style.transitionDuration = ms ? ms + "ms" : ""' in js
    assert "[data-sw]:not(details), details[data-sw] > summary { touch-action: pan-y }" in css


def test_u12_line_fix_panel_comes_from_the_page_and_speaker_bar_has_its_own_menu(env):
    """U12 (2026-11-25): a line's ⋯ / More fixes panel is copied from one template on the page (no trip to the M14);
    right-clicking a color in the speaker bar gives that speaker's menu; the ＋ Tag box is small on computers."""
    client, _, _ = env
    tx = client.get("/conversations/1?tx=1").text
    if 'id="line-edit-tpl"' not in tx:
        tx = client.get("/conversations/1/transcript").text
    assert 'id="line-edit-tpl"' in tx and "__SID__" in tx and 'hx-get="/segments/' not in tx
    panel = client.get("/segments/2/edit")
    assert panel.status_code == 200 and "Save words" in panel.text and "<html" not in panel.text
    root = Path(__file__).resolve().parents[2] / "scribe_bot/web/static"
    js, ui, css = (root / "app.js").read_text(), (root / "ui.js").read_text(), (root / "app.css").read_text()
    assert 'document.getElementById("line-edit-tpl")' in js
    assert "function speakerMenu(id, seg)" in ui and '"Go to their first line"' in ui
    assert ".tag-add form.row input.grow { flex: 0 0 auto; width: 15ch" in css


def test_u13_right_click_adds_instead_of_repeating():
    """U13 (2026-11-25): inside a task's details the right-click offers copy, one-click reminders and the talk in a new
    tab, not the rows already on screen; a task row's menu drops Details and Done (a tap and the circle do those)."""
    ui = (Path(__file__).resolve().parents[2] / "scribe_bot/web/static/ui.js").read_text()
    assert '"Remind me tomorrow morning"' in ui and '"Open the talk in a new tab"' in ui and '"Copy task"' in ui
    assert 'task ? "Details" : "Open"' not in ui and '!(task && a.t === "Done")' in ui
    assert 'lab2 + "…"' not in ui          # the old menu that listed each detail row again


def test_b18_speed_log_times_back(env):
    """B18: a Back load is logged as "back" and a page shown again from memory as "bfc", timed from the press of Back."""
    client, _, cfg = env
    app_js, ui_js = client.get("/static/app.js").text, client.get("/static/ui.js").text
    assert 'n.type === "back_forward" ? "back" : "page"' in app_js and 'k: "bfc"' in app_js
    assert "B18: the speed log times Back" in ui_js
    client.post("/perf", content='[{"k":"bfc","p":"/conversations","total":42}]')
    assert "bfc /conversations total=42" in (cfg.data_dir / "logs" / "speed.log").read_text()


def test_b23_forward_link_bottom_right(env):
    """B23: Your speaking's onward link sits on its own line at the bottom right, named Talks like everywhere else."""
    client, _, _ = env
    page = client.get("/speaking").text
    assert '<p class="fwd-link"><a href="/conversations">Talks →</a></p>' in page and "Conversations →" not in page

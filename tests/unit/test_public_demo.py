"""The public web demo (Vercel): Keychain reads without a Keychain, and the made-up-meetings line."""
import pytest

from scribe_bot.core.secrets import KeychainStore, SecretMissing


def test_no_keychain_on_this_machine_reads_as_no_key():
    """Linux and the public demo on Vercel have no `security` command: Settings must show "No key", not crash."""
    def missing(*a, **k):
        raise FileNotFoundError("security")
    store = KeychainStore(run=missing)
    assert store.exists("anthropic") is False
    with pytest.raises(SecretMissing):
        store.get("anthropic")


def test_public_demo_line_shows_only_on_the_web_demo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scribe_bot import demo_data
    from scribe_bot.web import app as web
    assert demo_data.main(["x", str(tmp_path)]) == 0
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path))
    client = TestClient(web.create_app())
    assert "demo-note" not in client.get("/conversations").text
    monkeypatch.setitem(web.templates.env.globals, "public_demo", True)
    page = client.get("/conversations").text
    assert "Clickable demo with made-up meetings" in page and "#run-it-yourself" in page
    assert 'id="demo-welcome"' in page and "1-minute tour" in page   # D-12: the first-visit card (app.js shows it once)


# ---------- D-11 (Brian, 2026-11-25): "what happens if someone tries to?" Nothing runs, nothing leaves. ----------

@pytest.fixture
def demo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scribe_bot import demo_data
    from scribe_bot.core import db
    from scribe_bot.web import app as web
    assert demo_data.main(["x", str(tmp_path)]) == 0
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path))
    monkeypatch.setenv("SCRIBE_BOT_PUBLIC_DEMO", "1")
    monkeypatch.setitem(web.templates.env.globals, "public_demo", True)
    client = TestClient(web.create_app(), headers={"origin": "http://testserver"})
    from scribe_bot.core import settings
    client.db = lambda: db.connect(settings.load().db_path)
    return client


def jobs(client) -> int:
    with client.db() as conn:
        return conn.execute("SELECT count(*) FROM job").fetchone()[0]


def test_keychain_is_never_read_on_the_public_demo(monkeypatch):
    """A hard stop under every AI, Discord and backup call: even a Keychain that would answer is never asked."""
    monkeypatch.setenv("SCRIBE_BOT_PUBLIC_DEMO", "1")
    def would_answer(*a, **k):
        raise AssertionError("the Keychain was asked on the public demo")
    store = KeychainStore(run=would_answer)
    assert store.exists("anthropic") is False
    with pytest.raises(SecretMissing):
        store.get("anthropic")


@pytest.mark.parametrize("path, note", [
    ("/conversations/2/rerun", "demo-ai"), ("/conversations/2/detail", "demo-ai"), ("/conversations/2/compare", "demo-ai"),
])
def test_ai_buttons_end_in_a_note_and_queue_nothing(demo, path, note):
    before = jobs(demo)
    r = demo.post(path, data={"confirm": "1"}, headers={"referer": "http://testserver/conversations/2"}, follow_redirects=False)
    assert r.status_code == 303 and f"note={note}" in r.headers["location"]
    assert jobs(demo) == before
    page = demo.get(r.headers["location"]).text
    assert "Demo: the AI doesn&#39;t run here" in page or "Demo: the AI doesn't run here" in page


def test_confirm_step_says_nothing_is_sent(demo):
    assert "nothing is sent or charged" in demo.get("/conversations/2/rerun/confirm").text


def test_upload_saves_nothing(demo, tmp_path):
    r = demo.post("/capture/upload", files={"file": ("memo.m4a", b"0" * 2000, "audio/mp4")},
                  headers={"accept": "application/json"})
    assert r.json() == {"results": [{"name": "memo.m4a", "result": "demo"}]}
    assert not (tmp_path / "uploads").exists() or not any((tmp_path / "uploads").iterdir())
    r = demo.post("/capture/upload", files={"file": ("memo.m4a", b"0" * 2000, "audio/mp4")}, follow_redirects=False)
    assert "note=demo-upload" in r.headers["location"]


def test_keys_and_sends_end_in_a_note(demo):
    with demo.db() as conn:
        accounts = conn.execute("SELECT count(*) FROM account").fetchone()[0]
        sent = conn.execute("SELECT count(*) FROM notification").fetchone()[0]
    r = demo.post("/settings/accounts", data={"provider": "anthropic", "nickname": "x"}, follow_redirects=False)
    assert "note=demo-keys" in r.headers["location"]
    r = demo.post("/notifications/test", follow_redirects=False)
    assert "note=demo-send" in r.headers["location"]
    with demo.db() as conn:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == accounts
        assert conn.execute("SELECT count(*) FROM notification").fetchone()[0] == sent


def test_the_real_app_is_unchanged(tmp_path, monkeypatch):
    """Without SCRIBE_BOT_PUBLIC_DEMO (the M14), Summarize still queues the AI run."""
    from fastapi.testclient import TestClient
    from scribe_bot import demo_data
    from scribe_bot.web import app as web
    assert demo_data.main(["x", str(tmp_path)]) == 0
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path))
    monkeypatch.delenv("SCRIBE_BOT_PUBLIC_DEMO", raising=False)
    client = TestClient(web.create_app(), headers={"origin": "http://testserver"})
    r = client.post("/conversations/2/rerun", data={"confirm": "1"}, follow_redirects=False)
    assert "rerun=queued" in r.headers["location"]



def test_avatar_initials_skip_brackets():
    """F6 (demo pass, 2026-11-25): "Lena Fischer (demo)" showed "L(" in its circle on Today's Late promises."""
    from scribe_bot.web import app as web
    m = web.templates.env.get_template("macros.html").module
    assert ">LF<" in str(m.avatar("Lena Fischer (demo)"))
    assert ">J<" in str(m.avatar("Jordan (demo)"))
    assert ">?<" in str(m.avatar(None))


def test_demo_footer_says_made_up(demo):
    page = demo.get("/").text
    assert "Demo · made-up data" in page and "Runs on the M14" not in page


def test_confirm_goes_back_to_the_talk(demo):
    """F12 (live pass): Confirm posts from the confirm page, so "back" stayed on the confirm box with the note."""
    r = demo.post("/conversations/2/rerun", data={"confirm": "1"},
                  headers={"referer": "http://testserver/conversations/2/rerun/confirm"}, follow_redirects=False)
    assert r.headers["location"].startswith("/conversations/2?") and "note=demo-ai" in r.headers["location"]


def test_today_unsure_matches_the_unsure_tab(demo):
    """F13 (live pass): Today said "0 unsure" while Tasks › Unsure listed 2 (the seed flagged tasks without review items)."""
    import re
    today = demo.get("/").text
    tab = demo.get("/tasks?tab=inbox").text
    n_today = int(re.search(r'href="/tasks\?tab=inbox"><b>(\d+)</b><small>unsure', today)[1])
    n_tab = int(re.search(r'Unsure\s*<span[^>]*>(\d+)<', tab)[1])
    assert n_today == n_tab >= 2


def test_reading_pane_leaves_the_list_room_at_medium_widths():
    """F14 (live pass): at 1024 wide the Right pane took 46vw beside the 256 px sidebar and the list shrank to ~230 px.
    Both panes' default widths and the drag clamps now leave the list ~460 px."""
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static"
    css, js = (static / "app.css").read_text(), (static / "ui.js").read_text()
    assert "--pane-w: clamp(300px, 46vw, calc(100vw - var(--side) - 460px))" in css
    assert "clamp(300px, 42vw, min(460px, calc(100vw - var(--side) - 460px)))" in css
    assert js.count("Math.max(300, Math.min(w, innerWidth - sideW() - 460))") == 2
    # at 1024 with the 256 px sidebar: the pane falls back to its 300 px floor, leaving the list 468 px
    assert 1024 - 256 - max(300, min(0.46 * 1024, 1024 - 256 - 460)) >= 460


def test_ai_settings_show_the_real_all_time_spend(demo):
    """F15 (live pass): Settings › AI & processing said "$0.78 this month · $0.00 all time" (all_time was never passed)."""
    import re
    page = demo.get("/settings/ai").text
    month = float(re.search(r"AI spend: \$([\d.]+) of \$\d+ this month", page)[1])
    total = float(re.search(r"\$([\d.]+) all time", page)[1])
    assert total >= month and total > 0


def test_pages_pass_every_value_their_templates_need(demo):
    """F15 sweep: a value a template reads but its route never passed renders as nothing (the $0.00 all-time spend).
    These names are optional on purpose; anything new here is a page showing a blank or a wrong zero."""
    import collections
    from jinja2 import Undefined, utils
    from scribe_bot.web import app as web
    seen, cur = collections.defaultdict(set), [None]

    class Rec(Undefined):
        def _note(self):
            if self._undefined_obj is utils.missing:
                seen[self._undefined_name].add(cur[0])
        def __str__(self): self._note(); return ""
        def __bool__(self): self._note(); return False
        def __iter__(self): self._note(); return iter(())
        def __hash__(self): return 0
    old = web.templates.env.undefined
    web.templates.env.undefined = Rec
    try:
        for p in ["/", "/inbox", "/conversations", "/conversations/2", "/people", "/people/2", "/tasks", "/tasks?tab=inbox",
                  "/search?q=budget", "/notifications", "/settings", "/settings/ai", "/settings/keys", "/settings/privacy",
                  "/settings/glossary", "/settings/templates", "/settings/backups", "/costs", "/status", "/capture", "/review"]:
            cur[0] = p
            assert demo.get(p).status_code == 200, p
    finally:
        web.templates.env.undefined = old
    assert set(seen) <= {"common", "on_today", "people", "trash"}, dict(seen)


def test_demo_has_one_claude_account(demo):
    """F16 (live pass): Accounts & keys listed the install's "Brian personal" and the seed's own Claude account side by side."""
    with demo.db() as conn:
        assert conn.execute("SELECT count(*) FROM account WHERE provider = 'anthropic'").fetchone()[0] == 1
    assert "Personal API (demo)" in demo.get("/settings/keys").text


def test_demo_glossary_has_terms_and_a_suggestion(demo):
    """F18 (live pass): Settings › Glossary was empty on the demo."""
    page = demo.get("/settings/glossary").text
    assert "Northwind" in page and "No terms yet" not in page
    assert "Glossary suggestions" in demo.get("/review").text


def test_settings_menu_is_icons_at_medium_widths():
    """F17 (live pass): at 1024 wide Settings had three columns and the Data Ledger rows ran 8 lines tall.
    F16: the "·" on a settings row sat on the word before it."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    assert "@media (min-width: 960px) and (max-width: 1279px)" in css
    assert "grid-template-columns: 52px minmax(0, 1fr)" in css
    assert ".settings-pane .item-sub > :is(span, b):not(.badge) + :is(span, b):not(.badge)::before { margin: 0 4px 0 0 }" in css


def test_demo_backups_and_schedules_have_rows(demo):
    """F19 (live pass): Settings › Backups & schedules said "No backups yet" and "No schedules yet" on the demo."""
    page = demo.get("/settings/backups").text
    assert "No backups yet" not in page and "No schedules yet" not in page
    assert "Drive A (demo)" in page and "Encrypted backup" in page


def test_costs_folds_look_like_the_other_sections(demo):
    """F20 (live pass): Costs' "Update from the Anthropic console" and "Every call" used a big heading; the rest use the
    small section header. F21: the last "·" on a row's second line was cut in half (clipped with its item)."""
    page = demo.get("/costs").text
    assert page.count('<details class="fold-sec"') == 2 and 'class="section transcript"' not in page
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    assert ".item:not(.conv-row) .item-sub > :is(span, b):not(.badge):not(:first-child):last-child { padding-left: 2px; margin-left: -2px }" in css


def test_status_page_reads_healthy_on_the_demo(demo):
    """F22 (live pass): the demo's Status page said "Needs attention", "Worker never started", "No readings yet" and
    "Worker not running · Web not running" (there's no worker or M14 behind it). Made-up healthy readings stand in."""
    page = demo.get("/status").text
    assert "● Healthy" in page and "Everything looks normal" in page
    assert "Needs attention" not in page and "never started" not in page and "No readings yet" not in page
    card = demo.get("/status/machine").text
    assert "not running" not in card and "Verified" in card
    h = demo.get("/health").json()
    assert h["ok"] and h["worker"]["alive"] and h["disk"]["ok"]


def test_closed_charts_keep_their_own_height():
    """F23 (live pass): opening one Status chart stretched the closed one beside it into an empty box."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    rule = next(line for line in css.splitlines() if line.startswith(".charts {"))
    assert "align-items: start" in rule


def test_demo_private_talk_has_no_ai_and_captures_show(demo):
    """F25: the demo's Private talk had AI notes and an AI task in Review (private talks never reach the AI).
    F24: Add recording › Recent said "Nothing captured yet" on the demo."""
    conn = demo.db()
    try:
        cid, summary = conn.execute("SELECT id, summary FROM conversation WHERE private = 1").fetchone()
        assert summary is None
        assert conn.execute("SELECT count(*) FROM task WHERE conversation_id = ?", (cid,)).fetchone()[0] == 0
    finally:
        conn.close()
    assert "Nothing captured yet" not in demo.get("/capture").text


def test_demo_search_finds_tasks_decisions_and_people(demo):
    """F27 (live pass): Search on the demo found only transcript lines (tasks, decisions, notes and people weren't indexed)."""
    assert "about the budget" in demo.get("/search?q=finance&type=task").text     # the matched word is in <mark>
    assert "for the first version" in demo.get("/search?q=simple&type=decision").text
    assert "Tasks 0" not in demo.get("/search?q=finance").text


def test_talk_menu_does_not_say_local_and_show_ai_cost(demo):
    """F28: the ⋯ menu said "Processed only on the M14 · AI cost $0.029" on a demo talk."""
    html = demo.get("/conversations/2").text
    assert "Processed only on the M14 ·" not in html
    assert "Made-up talk" in html


def test_task_pane_values_stay_on_the_row():
    """F29: in the narrow right-hand task pane, "Due ... Nov 18 ›" wrapped onto two lines (.grow's 10rem minimum)."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    assert ".ts-pick .grow, .ts-row > summary .grow, a.ts-row .grow { min-width: 0 }" in css


def test_long_owner_name_does_not_cut_the_due_date(demo):
    """F30: on Waiting, "Dr. Evangeline Montgomery-Whitfield (demo)" pushed "Overdue · due Nov 21" off the row."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    assert ".item-sub > b:not(:last-child) { flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; min-width: 4em }" in css
    assert ".item-sub > :last-child:not(:first-child):not(.due-v) { flex-shrink: 100 !important }" in css
    assert 'class="due-v bad"' in demo.get("/tasks?tab=waiting").text   # the date is marked so it never shrinks


def test_decisions_put_a_dot_between_date_and_talk(demo):
    """F31: Decisions read "Nov 25  DEMO: Product kickoff" with no "·" between the date and the talk."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    assert '.item-sub > :is(span, b):not(.badge) + a::before { content: "·"' in css
    html = demo.get("/tasks?tab=decisions").text
    import re
    assert re.search(r'<span>[^<]+</span><a href="/conversations/', html)   # the date span, then the talk link


def test_inbox_total_says_which_recordings(demo):
    """F32: "All 7: with AI ~80 min" under "9 waiting" read as a miscount and didn't say what the times were."""
    html = demo.get("/inbox").text
    assert 'in-total">All ' not in html
    import re
    assert re.search(r'class="meta in-total">To do all \d+ new ones: Summarize ~[^<]+ \(~\$[\d.]+\), or Transcribe ~[^<]+ \(free\)</p>', html)   # F57 renamed


def test_last_backup_is_todays(demo):
    """F33: Settings said "Last backup OK · Thu Nov 19" on Nov 25: the made-up week was saved newest first."""
    import re
    conn = demo.db()
    newest = conn.execute("SELECT max(finished_at) FROM backup_run WHERE kind = 'backup'").fetchone()[0]
    last_by_id = conn.execute("SELECT finished_at FROM backup_run WHERE kind = 'backup' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert last_by_id == newest
    assert re.search(r"Last backup OK · ", demo.get("/settings").text)


def test_settings_rows_space_the_dot_evenly(demo):
    """F34: Backups read "Backup· Wed Nov 25· 0.04 GB": settings rows are plain text (they wrap in full), so the "·"
    only gets its space before it when the parts have a space between them. Every settings page, every row."""
    import re
    from scribe_bot.web.routes import SETTINGS_GROUPS
    for group in SETTINGS_GROUPS:
        html = demo.get(f"/settings/{group}").text
        for sub in re.findall(r'<div class="item-sub">(.*?)</div>', html, re.S):
            assert not re.search(r"</(span|b)><(span|b)\b", sub), (group, sub[:120])


def test_schedules_last_ran_at_their_own_time(demo):
    """F35: "Encrypted backup · daily 2:00 AM · last ran Wed Nov 25, 11:05 PM": each made-up run now sits at its job's time."""
    import re
    html = demo.get("/settings/backups").text
    rows = re.findall(r"<span>(?:daily|monthly|Sundays) (\d+:\d\d [AP]M)</span>\s*<span>last ran [^<]*?, (\d+:\d\d [AP]M)</span>", html)
    assert len(rows) == 9
    assert all(when == ran for when, ran in rows), rows
    backups = re.findall(r"<span>Backup</span> <span>[^<]*?, (\d+:\d\d [AP]M)</span>", html)
    assert backups and set(backups) == {"2:00 AM"}


def test_ai_spend_line_lines_up_with_its_name(demo):
    """F36: AI & processing › AI spend: "$2.34 all time…" sat 32 px right of "AI spend…" (an indent meant for icon rows)."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    assert ".settings-pane .tap-row .item-sub { padding-left: 32px }" not in css
    assert ".settings-pane .tap-row:has(.item-link > .icon) .item-sub { padding-left: 32px }" in css
    assert 'id="costs"><div class="item-main"><a class="item-link" href="/costs">AI spend' in demo.get("/settings/ai").text


def test_note_under_a_card_has_a_gap(demo):
    """F37: AI & processing: "Saves as soon as you tap…" touched the "If it fails" card above it."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    assert ".card + p.meta { margin-top: var(--s2) }" in css
    import re
    assert re.search(r'</div>\s*</div>\s*<p class="meta">Saves as soon as you tap', demo.get("/settings/ai").text)


def test_add_account_form_says_me_on_the_demo(demo):
    """F38: Accounts & keys › Add an account filled Owner with a real first name on the public demo."""
    html = demo.get("/settings/keys").text
    assert 'name="owner" value="Me"' in html and 'value="Brian"' not in html


def test_public_demo_has_no_remove_demo_data_button(demo):
    """F39: Help › Try it safely offered "Remove demo data" on the public demo, where everything is made up (and it failed)."""
    html = demo.get("/help").text
    assert "Remove demo data" not in html and "Open the demo" in html


def _static(name):
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / name).read_text()


def test_reading_pane_follows_the_list_after_the_page_settles():
    """F40: Tasks at 1024 px with Reading pane = Right: the pane sat ~14 px above the list (touching the Sort and Person
    pickers) until the first scroll, because it was measured once before the page above the list finished growing."""
    js = _static("ui.js")
    assert "new ResizeObserver(later)" in js and "document.fonts.ready.then(later)" in js
    assert 'addEventListener("load", later)' in js


def test_late_promise_dates_never_shrink(demo):
    """F41: Today › Late promises to you, reading pane open: "Overdue · due No…". The date is the row's last item there."""
    import re
    assert ".item-sub > .due-v { flex: none !important }" in _static("app.css")
    html = demo.get("/").text
    sec = html.split("Late promises to you", 1)[1].split("</section>", 1)[0]
    assert re.search(r'<div class="item-sub"><b>[^<]+</b><span class="due-v', sec)


def test_open_and_open_in_new_tab_have_different_icons():
    """F42: right-click a talk: "Open" and "Open in new tab" showed the same arrow-out-of-a-box icon."""
    import re
    js = _static("ui.js")
    for m in re.finditer(r'(?:item\("(\w+)", "([^"]+)"|\{ i: "(\w+)", t: "([^"]+)")', js):
        icon, label = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if label.startswith("Open"):
            assert (icon == "open") == ("new tab" in label), (icon, label)


def test_open_full_page_has_the_same_tab_icon():
    """F50: the preview's "Open full page" opens in this tab but wore the ↗ new-tab icon; it now has the › of every Open."""
    import pathlib
    base = pathlib.Path("scribe_bot/web/templates/base.html").read_text()
    link = base.split("data-rpane-full")[1].split("</a>")[0]
    assert 'icon("chev")' in link and 'icon("open")' not in link


def test_me_label_never_doubles_up():
    """People showed "Me (me)" when person 1 was named "me"."""
    from scribe_bot.web.app import me_label
    assert [me_label(n) for n in (None, "", "Me", "me", " ME ", "Myself", "Sam")] == ["Me"] * 6 + ["Me (Sam)"]


def test_quick_settings_names_the_pane_choice_like_the_preview(demo):
    """F44: Talks/People › Quick settings › Reading pane read "None"; the preview's own switch says "Full page"."""
    for path in ("/conversations", "/people"):
        html = demo.get(path).text
        assert 'qs-t-none"' in html and "</i>Full page</button>" in html and "</i>None</button>" not in html
    assert "</i>Pop up</button>" in demo.get("/tasks").text


def test_demo_note_not_repeated_inside_the_preview(demo):
    """F46: People › pick a person with Reading pane = Below: the preview repeated "Clickable demo with made-up meetings"."""
    assert "Clickable demo with made-up" in demo.get("/people/5").text
    assert "Clickable demo with made-up" not in demo.get("/people/5", headers={"sec-fetch-dest": "iframe"}).text


def test_times_say_am_pm_the_same_way_everywhere(demo):  # noqa: F811
    """F48: the snooze menu said "8 AM" and the badge under it "back tomorrow 12:57 am"; Settings said "daily 2:00 AM · last
    ran 2:00 am". Every time the app writes says AM/PM, like the fixed labels (and Brian's "7 AM")."""
    import re
    for url in ("/settings/backups", "/review", "/conversations", "/notifications?tab=all", "/status", "/processing", "/inbox"):
        html = re.sub(r"<script.*?</script>|<style.*?</style>", "", demo.get(url).text, flags=re.S)
        assert not re.findall(r"\d:\d\d (?:am|pm)\b", html), (url, re.findall(r".{30}\d:\d\d (?:am|pm)\b", html)[:3])


def test_snoozed_badge_sits_on_its_own_line():
    """F47: "Snoozed · back tomorrow 12:57 AM" ran straight into the item's name; every snoozed badge sits on its own line."""
    import pathlib
    review = pathlib.Path("scribe_bot/web/templates/_review.html").read_text()
    assert review.count("Snoozed · back") == review.count('badge-outline snz">Snoozed · back') == 3
    assert ".item-main > .snz { display: flex;" in _static("app.css")


def test_review_names_its_own_open_tab(demo):  # noqa: F811
    """F49: Review's Dismissed and Snoozed tabs said "under Needs you", a section that's on Today; here the tab is Open."""
    for tab in ("dismissed", "snoozed", "done"):
        html = demo.get(f"/review?tab={tab}").text
        assert "under Needs you" not in html and "Needs you item" not in html, tab
    assert "back in Open" in demo.get("/review?tab=dismissed").text


def test_the_talk_being_transcribed_shows_as_processing(demo):
    """F84 (500 pass): the demo's jobs were kind 'process', which nothing reads, so Status said "Nothing processing"
    beside "1 running", and no page showed the recording that was being transcribed."""
    page = demo.get("/status").text
    assert "Nothing processing" not in page and "1 running" in page
    assert "Recording Nov 25 at 8:10 AM" in page and "Step 2 of 7</b> · Transcribing" in page


def test_a_not_found_page_keeps_the_header_counts(demo):
    """F85 (500 pass): "Not here" was drawn without the database, so the Today and Inbox counts, the bell's count and the
    status dot vanished from its header (a More menu stood in for the dot), as if they had been cleared."""
    import re
    def header(page):
        bell = re.search(r'aria-label="(Notifications[^"]*)"', page).group(1)
        return bell, re.findall(r'aria-label="(\d+ (?:need you|waiting))"', page), 'Status:' in page
    today, missing = demo.get("/").text, demo.get("/nope", headers={"accept": "text/html"})
    assert missing.status_code == 404 and "Not here" in missing.text
    assert header(missing.text) == header(today) and header(today)[2] and len(header(today)[1]) == 2

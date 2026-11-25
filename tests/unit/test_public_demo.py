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

"""Costs page, per-conversation cost, Help page, demo data (synthetic only)."""
import re
from test_web_pages import env, one  # noqa: F401


def test_costs_page_itemises_by_conversation(env):
    client, conn, _ = env
    page = client.get("/costs").text
    assert "Weekly sync with Alex" in page and "$0.070" in page and "Claude Sonnet" in page
    assert "All time" in page and "This month" in page and "monthly cap" in page
    assert 'href="https://console.anthropic.com/settings/usage" target="_blank" rel="noopener noreferrer"' in page


def test_conversation_shows_its_own_cost(env):
    assert "AI cost $0.070" in env[0].get("/conversations/1").text


def test_costs_and_help_are_in_navigation(env):
    page = env[0].get("/more").text
    assert 'href="/costs"' in page and 'href="/help"' in page and 'href="/settings"' in page


def test_theme_from_cookie_and_gear_everywhere(env):
    client = env[0]
    page = client.get("/").text
    assert '<html lang="en" data-density' in page and "data-theme-toggle" not in page     # default: follow the system; Dark mode is in Settings only
    assert page.count('summary class="icon-btn" aria-label="Quick settings"') == 2 and page.count('href="/settings"') >= 3   # ⚙ Quick settings (phone + desktop) with All settings
    client.cookies.set("theme", "dark")
    dark = client.get("/tasks").text
    assert '<html lang="en" data-theme="dark" data-density' in dark                        # rendered server-side: no flash
    assert '<meta name="theme-color" content="#1c1917">' in dark and "prefers-color-scheme" not in dark.split("</head>")[0]
    client.cookies.set("theme", "light")
    assert '<meta name="theme-color" content="#fafaf9">' in client.get("/").text   # phone status bar follows the pick
    client.cookies.set("theme", "evil\"><script>")
    assert "<script>" not in client.get("/").text.split("<head>")[0]


def test_help_page_and_demo_round_trip(env):
    client, conn, _ = env
    page = client.get("/help").text
    assert "What each screen is for" in page and "Load demo data" in page
    r = client.post("/demo/load", follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    demo = client.get(f"/conversations/{cid}").text
    assert "DEMO: Product kickoff" in demo and "Jordan Sample (demo)" in demo
    client.post("/demo/load")                                        # idempotent
    assert one(conn, "SELECT count(*) FROM conversation WHERE title LIKE 'DEMO:%'") == 1
    assert "Remove demo data" in client.get("/help").text
    before = one(conn, "SELECT count(*) FROM task WHERE conversation_id = 1")
    client.post("/demo/remove")
    assert one(conn, "SELECT count(*) FROM conversation WHERE title LIKE 'DEMO:%'") == 0
    assert one(conn, "SELECT count(*) FROM person WHERE name LIKE '%(demo)'") == 0
    assert one(conn, "SELECT count(*) FROM task WHERE conversation_id = 1") == before   # real data untouched


def test_shortcuts_listed_once_and_shown_in_both_places(env):
    client = env[0]
    for path in ("/", "/help"):
        page = client.get(path).text
        assert "Fix the highlighted line" in page and "Copy the summary" in page and "<kbd>Esc</kbd>" in page
    help_page = client.get("/help").text
    assert help_page.count("Fix the highlighted line") == 2                       # Help section + the "?" dialog


def test_tour_starts_from_help_and_today_and_prefers_the_demo(env):
    client, conn, _ = env
    assert 'data-tour-start="1"' in client.get("/help").text and 'data-tour-start="1"' in client.get("/").text
    assert 'id="tour"' in client.get("/tasks").text and 'id="check-strip"' in client.get("/conversations/1").text
    client.post("/demo/load")
    demo = one(conn, "SELECT id FROM conversation WHERE title LIKE 'DEMO:%'")
    assert f'data-tour-start="{demo}"' in client.get("/help").text                 # the made-up one, not a real meeting


def test_page_code_links_carry_a_version(env):
    page = env[0].get("/").text
    v = re.search(r'/static/app\.js\?v=([0-9a-f]{10})"', page)
    assert v and f"/static/app.css?v={v.group(1)}" in page                         # a new deploy = new URLs = no stale code
    assert env[0].get(f"/static/app.js?v={v.group(1)}").status_code == 200
    # Every link is a full page load since 2026-11-22 (no hx-boost), so a new deploy's ?v= reaches every page by itself.
    # The old "newer page code" header and its swap check went with boost (R5-20).


def test_remove_demo_after_its_person_was_used_in_a_real_talk(env):
    """F39 (edge case): the demo person picked as a speaker in a real talk, and given a real task. "Remove demo data" used
    to fail ("changed on another device", the speaker still pointed at them) and would have deleted the real task."""
    client, conn, _ = env
    client.post("/demo/load")
    pid = one(conn, "SELECT id FROM person WHERE name = 'Jordan Sample (demo)'")
    real = one(conn, "SELECT id FROM conversation WHERE title NOT LIKE 'DEMO:%' ORDER BY id LIMIT 1")
    conn.execute("UPDATE speaker SET person_id = ?, confirmed = 1 WHERE id = (SELECT id FROM speaker WHERE conversation_id = ? LIMIT 1)",
                 (pid, real))
    conn.execute("INSERT INTO task (conversation_id, title, owner_person_id, created_at, updated_at) "
                 "VALUES (?, 'Real task', ?, '2026-11-25T10:00:00+00:00', '2026-11-25T10:00:00+00:00')", (real, pid))
    conn.commit()
    r = client.post("/demo/remove", follow_redirects=False)
    assert r.status_code == 303
    assert one(conn, "SELECT count(*) FROM person WHERE name LIKE '%(demo)'") == 0
    assert one(conn, "SELECT owner_person_id FROM task WHERE title = 'Real task'") == 1
    assert one(conn, "SELECT count(*) FROM speaker WHERE conversation_id = ? AND person_id IS NULL", real) >= 1

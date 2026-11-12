"""Settings page: grouped sections, theme picker, plain-English schedules (synthetic only)."""
from test_web_pages import env  # noqa: F401

from scribe_bot.backup.jobs import SCHEDULES as BACKUP_SCHEDULES
from scribe_bot.core.cleanup import SCHEDULES as CLEANUP_SCHEDULES
from scribe_bot.discord_bot.briefing import BRIEFING, WEEKLY
from scribe_bot.pipeline.recording import make_schedules
from scribe_bot.web.routes import SCHEDULE_INFO


def test_settings_home_is_short_and_each_group_has_its_page(env):
    client = env[0]
    home = client.get("/settings").text.split('id="content"')[1]                 # the page itself, not the header menus
    hrefs = ["/settings/appearance", "/settings/ai", "/settings/keys", "/settings/privacy", "/settings/glossary",
             "/settings/templates", "/settings/backups"]
    assert [home.index(f'href="{h}"') for h in hrefs] == sorted(home.index(f'href="{h}"') for h in hrefs)
    assert 'href="/notifications"' not in home and 'href="/search/history"' not in home   # 🔔 and Search are their homes
    assert 'id="keys"' not in home and 'id="schedules"' not in home                 # the groups live on their own pages
    for group, anchor in (("appearance", "appearance"), ("ai", "ai"), ("keys", "keys"), ("privacy", "privacy"),
                          ("glossary", "glossary"), ("templates", "templates"), ("backups", "schedules")):
        assert f'id="{anchor}"' in client.get(f"/settings/{group}").text
    ai = client.get("/settings/ai").text
    assert 'class="item-link" href="/costs">AI spend' in ai and "Month-end forecast" not in ai   # the breakdown lives on /costs
    privacy = client.get("/settings/privacy").text
    assert privacy.count('action="/settings/offline"') == 1 and 'data-label="Cost"' in privacy
    assert "hx-confirm=\"Delete the glossary term" in client.get("/settings/glossary").text
    assert client.get("/settings/nope").status_code == 404
    assert client.get("/settings/status", follow_redirects=False).headers["location"] == "/status"   # old Discord links


def test_theme_picker_marks_the_current_choice(env):
    client = env[0]
    assert 'data-theme-set="system" class="on"' in client.get("/settings/appearance").text
    client.cookies.set("theme", "dark")
    page = client.get("/settings/appearance").text
    assert 'data-theme-set="dark" class="on"' in page and 'data-theme-set="system" class=""' in page


def test_schedules_have_plain_names_and_never_run_says_so(env):
    client, conn, _ = env
    conn.execute("INSERT INTO schedule_run (job_name, last_success_at, status) VALUES ('backup', '2026-10-19T14:51:00', 'new'), "
                 "('briefing', '2026-10-20T07:00:00-04:00', 'ok'), ('reconcile', NULL, 'failed: disk full')")
    sched = client.get("/settings/backups").text.split('id="schedules"')[1]
    assert "Encrypted backup" in sched and "daily 2:00 AM" in sched and "not run yet" in sched and "Waiting" in sched
    from scribe_bot.web.app import local_datetime
    assert "Morning briefing" in sched and "last ran " + local_datetime("2026-10-20T07:00:00-04:00") in sched   # local time
    assert 'title="failed: disk full">Failed' in sched
    assert sched.index("Encrypted backup") < sched.index("Morning briefing")          # in the order they run


def test_every_real_schedule_has_a_plain_name(env):
    names = {s.name for s in [*BACKUP_SCHEDULES, *CLEANUP_SCHEDULES, BRIEFING, WEEKLY, *make_schedules(env[1])]}
    assert names == set(SCHEDULE_INFO)

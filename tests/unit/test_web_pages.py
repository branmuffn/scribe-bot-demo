"""Web app pages and actions against a temp database seeded with synthetic rows (never real data)."""
import json
import subprocess
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from scribe_bot.core import db
from scribe_bot.core.settings import Settings
from scribe_bot.web.app import create_app

PMSET = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t90%; charged; 0:00 remaining present: true\n"
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


class FakeKeys:
    def exists(self, provider, account="brian-personal"):
        return provider == "anthropic"


def fake_pmset(args, **kw):
    return subprocess.CompletedProcess(args, 0, stdout=PMSET, stderr="")


def seed(conn, data_dir):
    t = db.now()
    audio = data_dir / "vault" / "sample.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake audio bytes")
    clip = data_dir / "vault" / "clip.wav"
    clip.write_bytes(b"RIFF fake clip")
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, title, vault_path) "
                 "VALUES (1, 'recorder', '/x', 'abc', ?, 'Weekly sync', ?)", (t, str(audio)))
    conn.execute("INSERT INTO person (id, name, area, created_at) VALUES (2, 'Alex Example', 'work', ?)", (t,))
    conn.execute("INSERT INTO conversation (id, recording_id, title, started_at, duration_s, type, area, area_confidence, summary, "
                 "takeaways, processed_by, status, created_at, updated_at) VALUES (1, 1, 'Weekly sync with Alex', ?, 1800, '1:1', "
                 "'work', 0.8, 'Synthetic summary about the zephyr launch plan.', ?, 'brian:claude-sonnet', 'done', ?, ?)",
                 (t, json.dumps(["Launch moves to May"]), t, t))
    conn.execute("INSERT INTO speaker (id, conversation_id, cluster, person_id, talk_seconds) VALUES (1, 1, 1, 2, 1200)")
    conn.execute("INSERT INTO speaker (id, conversation_id, cluster, person_id, talk_seconds, clip_path) VALUES (2, 1, 0, 1, 500, NULL)")
    conn.execute("INSERT INTO speaker (id, conversation_id, cluster, talk_seconds, clip_path) VALUES (3, 1, 2, 100, ?)", (str(clip),))
    lines = [(0, 0.0, 1, "Hi Brian, thanks for making time."), (1, 4.0, 2, "Sure. Let's talk about the zephyr launch."),
             (2, 9.5, 1, "I'll send the budget by Friday."), (3, 15.0, 3, "Quick interruption from someone else.")]
    for idx, start, spk, text in lines:
        conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) VALUES (1, ?, ?, ?, ?, ?)",
                     (idx, start, start + 4, spk, text))
        conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('segment', ?, 1, ?)", (idx + 1, text))
    conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('summary', 1, 1, 'zephyr launch plan')")
    conn.execute("INSERT INTO task (id, conversation_id, title, owner_person_id, due, area, confidence, source_segment_id, created_at, updated_at) "
                 "VALUES (1, 1, 'Review the launch deck', 1, ?, 'work', 0.9, 2, ?, ?)", (YESTERDAY, t, t))
    conn.execute("INSERT INTO task (id, conversation_id, title, owner_person_id, area, confidence, source_segment_id, created_at, updated_at) "
                 "VALUES (2, 1, 'Send the budget', 2, 'work', 0.9, 3, ?, ?)", (t, t))
    conn.execute("INSERT INTO task (id, conversation_id, title, owner_person_id, area, confidence, inbox, created_at, updated_at) "
                 "VALUES (3, 1, 'Maybe book a venue', 1, NULL, 0.4, 1, ?, ?)", (t, t))
    conn.execute("INSERT INTO decision (id, conversation_id, text, source_segment_id, created_at) VALUES (1, 1, 'Launch moves to May', 2, ?)", (t,))
    conn.execute("INSERT INTO person_fact (id, person_id, conversation_id, type, value, source_segment_id, created_at) "
                 "VALUES (1, 2, 1, 'family', 'Has a dog named Biscuit', 1, ?)", (t,))
    conn.execute("INSERT INTO review_item (kind, conversation_id, ref_id, created_at) VALUES ('speaker', 1, 3, ?)", (t,))
    conn.execute("INSERT INTO review_item (kind, conversation_id, ref_id, payload, created_at) VALUES ('inbox', 1, 3, ?, ?)",
                 (json.dumps({"reason": "Owner unclear"}), t))
    conn.execute("INSERT INTO glossary_term (id, term, variants, created_at) VALUES (1, 'Zephyr', '[\"zefir\"]', ?)", (t,))
    conn.execute("INSERT INTO review_item (kind, ref_id, created_at) VALUES ('glossary', 1, ?)", (t,))
    conn.execute("INSERT INTO ledger_entry (time, account_id, provider, model, task, conversation_id, sent_summary, tokens_in, tokens_out, "
                 "cost_usd, training_policy) VALUES (?, 1, 'anthropic', 'claude-sonnet', 'extract', 1, 'transcript, 4 lines', 100, 50, 0.07, "
                 "'no training')", (t,))


@pytest.fixture
def env(tmp_path):
    cfg = Settings(tmp_path)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    seed(conn, tmp_path)
    client = TestClient(create_app(cfg, run=fake_pmset, secrets_store=FakeKeys()))
    yield client, conn, cfg
    conn.close()


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


@pytest.mark.parametrize("path", ["/", "/conversations", "/conversations/1", "/tasks", "/tasks?tab=waiting", "/tasks?tab=inbox",
                                  "/decisions", "/people", "/people/2", "/people/1", "/review", "/search?q=zephyr", "/capture",
                                  "/settings", "/status", "/manifest.webmanifest", "/sw.js", "/static/app.css", "/static/htmx.min.js"])
def test_every_page_renders(env, path):
    client, _, _ = env
    r = client.get(path)
    assert r.status_code == 200, path


def test_today_shows_only_what_needs_brian(env):
    client, conn, _ = env
    page = client.get("/").text
    assert "Review the launch deck" in page                                      # Brian's overdue task
    assert 'href="/speaking"' in page                                            # Your week › Your speaking
    assert "Send the budget" not in page                                         # someone else's, not late: lives in Tasks
    assert 'id="needs-you"' in page and "Needs you · 3" in page                    # Review lives on Today now
    assert "Speakers to name" in page and 'action="/speakers/' in page and "Glossary suggestions" in page
    assert "Cloud approvals" not in page                                          # empty groups are hidden
    assert "Speakers to name" in client.get("/review").text                       # old Discord links still open Review
    assert "Recent conversations" not in page and "AI spend" not in page          # one tap away, not on Today
    conn.execute("UPDATE task SET due = '2020-01-01' WHERE id = 2")                # now Alex is late
    assert "Late promises to you" in client.get("/").text and "Send the budget" in client.get("/").text


def test_today_all_clear_and_spend_alert(env):
    client, conn, _ = env
    conn.execute("UPDATE task SET status = 'done'")
    conn.execute("UPDATE review_item SET status = 'done'")
    assert "needs attention" in client.get("/").text and "All clear" not in client.get("/").text   # no worker heartbeat
    conn.execute("INSERT INTO setting (key, value) VALUES ('worker_heartbeat', ?)", (db.now(),))
    assert "All clear" in client.get("/").text
    conn.execute("INSERT INTO setting (key, value) VALUES ('global_cap_usd', '0.08')")   # $0.07 spent = 88%
    page = client.get("/").text
    assert "AI spend is $0.07 of the $0 monthly cap" in page and "All clear" not in page


def test_conversation_page_follows_output_design(env):
    page = env[0].get("/conversations/1").text
    order = [page.index(f"<h2>{h}</h2>") for h in ("Summary", "You owe", "They owe you", "Decisions", "People notes", "Transcript")]
    assert order == sorted(order)
    a, b = "Alex Example</span> 50%", "Brian</span> 25%"                # talk share from the lines, first speaker first
    assert a in page and b in page
    assert page.index(a) < page.index(b)
    assert "Sent to Claude" in page                                  # privacy badge
    assert 'href="#L1">line 2</a>' in page                             # item -> transcript line
    assert '<details class="section transcript" id="transcript">' in page   # collapsed
    assert 'data-seek="9.5"' in page                                   # tap to play
    assert "Has a dog named Biscuit" in page
    assert "Speaker 3" in page and "Who is this?" in page              # unnamed speaker can be named here


def test_area_toggle_filters_and_is_remembered(env):
    client, conn, _ = env
    r = client.get("/conversations?area=personal")
    assert "Weekly sync with Alex" not in r.text
    assert r.cookies.get("area") == "personal"
    assert "Weekly sync with Alex" not in client.get("/conversations").text   # cookie applies
    assert "Weekly sync with Alex" in client.get("/conversations?area=work").text


def test_change_area(env):
    client, conn, _ = env
    client.post("/conversations/1/area", data={"area": "personal"})
    assert one(conn, "SELECT area FROM conversation WHERE id = 1") == "personal"
    assert one(conn, "SELECT area_confidence FROM conversation WHERE id = 1") == 1
    assert one(conn, "SELECT count(*) FROM task WHERE conversation_id = 1 AND area = 'personal'") == 3


def test_mark_private_keeps_ai_notes_and_blocks_more_ai(env):
    client, conn, _ = env
    before = {t: one(conn, f"SELECT count(*) FROM {t} WHERE conversation_id = 1") for t in ("task", "decision", "person_fact")}
    client.post("/conversations/1/private")
    assert one(conn, "SELECT private FROM conversation WHERE id = 1") == 1
    assert one(conn, "SELECT private FROM recording WHERE id = 1") == 1
    assert one(conn, "SELECT summary FROM conversation WHERE id = 1") is not None          # kept (Brian 2026-10-22)
    assert {t: one(conn, f"SELECT count(*) FROM {t} WHERE conversation_id = 1") for t in before} == before
    from scribe_bot.ai import extract
    assert extract.estimate_cost(conn, 1)["blocked"]                                       # no more AI
    page = client.get("/conversations/1").text
    assert "Private conversation" in page and "badge-private" in page


def test_task_actions_feed_ratings(env):
    client, conn, _ = env
    client.post("/tasks/1/done")
    assert conn.execute("SELECT status, rated FROM task WHERE id = 1").fetchone()[:] == ("done", "right")
    client.post("/tasks/2/edit", data={"title": "Send the revised budget", "owner": 2, "due": ""})
    assert conn.execute("SELECT title, rated FROM task WHERE id = 2").fetchone()[:] == ("Send the revised budget", "wrong")
    client.post("/tasks/3/delete")
    assert conn.execute("SELECT status, rated FROM task WHERE id = 3").fetchone()[:] == ("dropped", "wrong")
    assert one(conn, "SELECT status FROM review_item WHERE kind = 'inbox'") == "dismissed"


def test_quick_add_and_inbox_filing(env):
    client, conn, _ = env
    client.post("/tasks", data={"title": "Call the plumber", "owner": 1, "due": "", "area": "personal"})
    row = conn.execute("SELECT owner_person_id, area, rated FROM task WHERE title = 'Call the plumber'").fetchone()
    assert row[:] == (1, "personal", None)
    assert "Call the <mark>plumber</mark>" in client.get("/search?q=plumber").text
    client.post("/tasks/3/file", data={"area": "work"})
    assert conn.execute("SELECT inbox, area FROM task WHERE id = 3").fetchone()[:] == (0, "work")
    assert one(conn, "SELECT status FROM review_item WHERE kind = 'inbox'") == "done"


def test_tabs_split_mine_waiting_inbox(env):
    client = env[0]
    assert "Review the launch deck" in client.get("/tasks?tab=mine").text
    waiting = client.get("/tasks?tab=waiting").text
    assert "Send the budget" in waiting and "Review the launch deck" not in waiting
    assert "Maybe book a venue" in client.get("/tasks?tab=inbox").text


def test_fact_confirm_and_reject(env):
    client, conn, _ = env
    client.post("/facts/1/confirm", data={"ok": 1})
    assert one(conn, "SELECT confirmed FROM person_fact WHERE id = 1") == 1
    client.post("/facts/1/confirm", data={"ok": 0})
    assert one(conn, "SELECT confirmed FROM person_fact WHERE id = 1") == 0
    assert "Biscuit" not in client.get("/people/2").text


def test_decision_marked_wrong_disappears(env):
    client, conn, _ = env
    client.post("/decisions/1/wrong")
    assert one(conn, "SELECT rated FROM decision WHERE id = 1") == "wrong"
    assert "Launch moves to May" not in client.get("/decisions").text


def test_name_speaker_new_person_and_enroll(env, monkeypatch):
    client, conn, _ = env
    calls = []
    monkeypatch.setattr("scribe_bot.web.routes.identify.enroll", lambda c, sid, pid, cfg=None: calls.append((sid, pid)))
    client.post("/speakers/3/name", data={"new_name": "Sam Sample"})
    pid = one(conn, "SELECT id FROM person WHERE name = 'Sam Sample'")
    assert conn.execute("SELECT person_id, confirmed FROM speaker WHERE id = 3").fetchone()[:] == (pid, 1)
    assert calls == [(3, pid)]
    assert one(conn, "SELECT status FROM review_item WHERE kind = 'speaker'") == "done"


def test_name_speaker_works_without_pipeline(env):
    client, conn, _ = env
    assert client.post("/speakers/3/name", data={"person_id": 2}).status_code == 200
    assert one(conn, "SELECT person_id FROM speaker WHERE id = 3") == 2


def test_review_glossary_approve(env):
    client, conn, _ = env
    rid = one(conn, "SELECT id FROM review_item WHERE kind = 'glossary'")
    client.post(f"/review/{rid}/approve")
    assert one(conn, "SELECT approved FROM glossary_term WHERE id = 1") == 1


def test_search_highlights_and_is_safe(env):
    client = env[0]
    page = client.get("/search?q=zephyr").text
    assert "<mark>zephyr</mark>" in page
    for q in ['"', "AND OR", "zep*) NOT (", "<script>alert(1)</script>"]:
        r = client.get("/search", params={"q": q})
        assert r.status_code == 200 and "<script>alert" not in r.text


def test_audio_only_from_database_paths(env, tmp_path):
    client, conn, cfg = env
    r = client.get("/audio/conversation/1")
    assert r.status_code == 200 and r.content == b"fake audio bytes"
    assert client.get("/audio/speaker/3").status_code == 200
    assert client.get("/audio/speaker/1").status_code == 404           # no clip
    outside = tmp_path.parent / "outside.m4a"
    outside.write_bytes(b"secret")
    conn.execute("UPDATE recording SET vault_path = ? WHERE id = 1", (str(outside),))
    assert client.get("/audio/conversation/1").status_code == 404      # outside the data folder
    conn.execute("UPDATE recording SET vault_path = ? WHERE id = 1", (str(cfg.data_dir / "vault" / ".." / ".." / "outside.m4a"),))
    assert client.get("/audio/conversation/1").status_code == 404
    assert client.get("/audio/conversation/99").status_code == 404


def test_conversation_audio_prefers_a_playable_file(env):
    client, conn, cfg = env
    caf = cfg.data_dir / "archive" / "x.caf"  # Opus-in-CAF: browsers can't play it
    caf.parent.mkdir(parents=True, exist_ok=True)
    caf.write_bytes(b"caf bytes")
    conn.execute("UPDATE recording SET archive_path = ? WHERE id = 1", (str(caf),))
    r = client.get("/audio/conversation/1")
    assert r.status_code == 200 and r.content == b"fake audio bytes" and r.headers["content-type"] == "audio/mp4"
    conn.execute("UPDATE recording SET vault_path = NULL WHERE id = 1")  # original gone after 30 days
    assert client.get("/audio/conversation/1").status_code == 404  # never send an unplayable file


def test_capture_note_upload_and_share(env):
    client, conn, cfg = env
    client.post("/capture/note", data={"text": "Remember the zephyr colours"})
    assert one(conn, "SELECT count(*) FROM capture WHERE kind = 'text'") == 1
    r = client.post("/capture/upload", files={"file": ("../../evil name.m4a", b"audio", "audio/mp4")})
    assert r.status_code == 200
    saved = list((cfg.data_dir / "uploads").iterdir())
    assert [p.name for p in saved] == ["evil name.m4a"]                 # folders stripped, kept in uploads/
    assert not (cfg.data_dir / "inbound").exists()                      # never into the Syncthing folder
    assert client.post("/capture/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
    client.post("/capture/share", data={"title": "Article", "text": "https://example.com/a"},
                files={"files": ("shot.png", b"\x89PNG", "image/png")})
    kinds = [r[0] for r in conn.execute("SELECT kind FROM capture ORDER BY id")]
    assert kinds == ["text", "link", "image"]


def test_settings_never_shows_keys_and_offline_switch(env):
    client, conn, _ = env
    keys = client.get("/settings/keys").text
    assert "Key stored" in keys and "set_api_key.sh" in keys
    for provider in ("Anthropic (Claude)", "Google (Gemini)", "OpenAI"):
        assert provider in keys
    assert 'type="password"' not in keys and 'name="key' not in keys and 'name="api' not in keys  # never a key field
    privacy = client.get("/settings/privacy").text
    assert "transcript, 4 lines" in privacy and "$0.07" in privacy
    assert 'href="/conversations/1"' in privacy                                   # each entry links to what was sent
    client.post("/settings/offline", data={"on": 1})
    assert one(conn, "SELECT value FROM setting WHERE key = 'ai_offline'") == "1"
    assert "On · AI blocked" in client.get("/settings/privacy").text


def test_glossary_add_and_delete(env):
    client, conn, _ = env
    client.post("/settings/glossary", data={"term": "Hexaware", "variants": "Hexterware, hex aware"})
    assert json.loads(one(conn, "SELECT variants FROM glossary_term WHERE term = 'Hexaware'")) == ["Hexterware", "hex aware"]
    gid = one(conn, "SELECT id FROM glossary_term WHERE term = 'Hexaware'")
    client.post(f"/settings/glossary/{gid}/delete")
    assert one(conn, "SELECT count(*) FROM glossary_term WHERE term = 'Hexaware'") == 0


def test_cross_site_posts_refused(env):
    client, conn, _ = env
    r = client.post("/tasks/1/done", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert one(conn, "SELECT status FROM task WHERE id = 1") == "open"
    assert client.post("/tasks/1/done", headers={"Origin": "http://testserver"}).status_code == 200


@pytest.mark.parametrize("headers, ok", [
    ({"Origin": "null"}, False),                                       # a no-referrer page or sandboxed frame elsewhere
    ({"Origin": "null", "Sec-Fetch-Site": "cross-site"}, False),
    ({"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}, False),
    ({"Sec-Fetch-Site": "same-site"}, False),                          # another machine's page, even in the tailnet
    ({"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}, True),
    ({}, True),                                                        # not a browser
])
def test_null_origin_posts_refused(env, headers, ok):
    """2026-10-22 review: "null" origins were let through, so any web page Brian opened (while on Tailscale) could post
    to the app: e.g. Run AI now on a Private conversation. Only the share target may post without an origin."""
    client, conn, _ = env
    r = client.post("/conversations/1/private", headers=headers, follow_redirects=False)
    assert (r.status_code != 403) == ok and one(conn, "SELECT private FROM conversation WHERE id = 1") == int(ok)


def test_share_target_still_accepts_the_phone(env):
    client, conn, _ = env
    for headers in ({"Sec-Fetch-Site": "none", "Origin": "null"}, {"Origin": "null"}):   # the share sheet (new / older Chrome)
        r = client.post("/capture/share", data={"text": "a synthetic note"}, headers=headers, follow_redirects=False)
        assert r.status_code == 303, headers
    assert one(conn, "SELECT count(*) FROM capture WHERE text = 'a synthetic note'") == 2


def test_trash_hides_everything_from_it_and_restores(env):
    """Its tasks, notes and review items go with it (lists, counts, Today), and come back on Restore or Undo."""
    client, conn, _ = env
    page = client.post("/conversations/1/delete").text
    assert one(conn, "SELECT deleted_at IS NOT NULL FROM conversation WHERE id = 1") == 1
    assert 'href="/conversations/1"' not in page                                              # gone from the list
    assert "Moved “Weekly sync with Alex” to Trash" in page and 'action="/undo/' in page      # Undo bar
    for path, gone in (("/tasks", "Review the launch deck"), ("/tasks?tab=waiting", "Send the budget"),
                       ("/people/2", "Has a dog named Biscuit"), ("/", "Speakers to name")):
        assert gone not in client.get(path).text, path
    assert 'Mine <span class="n">0</span>' in client.get("/tasks").text and 'aria-label="1 need you"' in client.get("/tasks").text
    assert "owes you" not in client.get("/people").text
    trash = client.get("/conversations?trash=1").text
    assert "Weekly sync with Alex" in trash and 'action="/conversations/1/restore"' in trash and "removed for good" in trash
    assert "This conversation is in Trash" in client.get("/conversations/1").text
    assert "Trash · 1" in client.get("/conversations").text
    client.post("/conversations/1/restore")
    assert "Review the launch deck" in client.get("/tasks").text and "Trash · " not in client.get("/conversations").text
    client.post("/conversations/1/delete")
    uid = one(conn, "SELECT max(id) FROM undo_action")
    client.post(f"/undo/{uid}")
    assert one(conn, "SELECT deleted_at FROM conversation WHERE id = 1") is None


def test_conversation_404(env):
    assert env[0].get("/conversations/999").status_code == 404


def test_person_profile_open_items_both_ways(env):
    page = env[0].get("/people/2").text
    assert "You owe Alex Example" in page and "Review the launch deck" in page
    assert "Alex Example owes you" in page and "Send the budget" in page
    assert "Weekly sync with Alex" in page


def test_manifest_has_share_target(env):
    m = env[0].get("/manifest.webmanifest").json()
    assert m["share_target"]["action"] == "/capture/share" and m["display"] == "standalone"


def test_design_basics(env):
    """Navigation on every page (sidebar + phone tab bar), Review count badge, More page, accessible skip link."""
    client = env[0]
    page = client.get("/tasks").text
    assert 'class="sidebar"' in page and 'class="tabbar"' in page and 'href="#content"' in page
    assert 'aria-label="3 need you"' in page   # speaker + inbox + glossary in the seed, counted on the Today tab
    assert page.count('class="tabbar"') == 1 and ">Talks" in page and "/more" not in page.split('class="tabbar"')[1]
    assert 'class="side-search"' in page and 'class="row-menu head-menu"' in page and "Dark mode" not in page
    for path, tab in (("/people/2", "/conversations"), ("/decisions", "/tasks"), ("/review", "/")):   # sub-pages light up their tab
        assert f'<a href="{tab}" class="on"' in client.get(path).text
    talks = client.get("/conversations").text
    assert 'href="/capture"' in talks and 'aria-label="Talks"' in talks and 'href="/people"' in talks   # ＋ and the People sub-tab
    r = client.get("/decisions", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/tasks?tab=decisions"
    assert 'id="decision-list"' in client.get("/tasks?tab=decisions").text
    more = client.get("/more").text
    for label in ("Status", "Costs", "Help", "Settings"):
        assert label in more


def test_empty_states(tmp_path):
    cfg = Settings(tmp_path)
    db.migrate(db.connect(cfg.db_path))
    client = TestClient(create_app(cfg, run=fake_pmset, secrets_store=FakeKeys()))
    assert "No conversations yet" in client.get("/conversations").text
    assert "All caught up" in client.get("/review").text
    assert "All clear" in client.get("/tasks").text
    assert "No Google (Gemini) account yet" in client.get("/settings/keys").text


def test_accounts_and_test_button_use_m3(env, monkeypatch):
    """Accounts & Keys shows M3's account list (key presence only); Test calls accounts.test_key."""
    client, conn, _ = env
    calls = []
    conn.execute("INSERT INTO account (id, provider, nickname, owner, type, training_policy, may_train, secret_ref, created_at) "
                 "VALUES (7, 'google', 'Family Gemini', 'Family', 'free', 'may train', 1, 'scribe-bot.google', ?)", (db.now(),))
    monkeypatch.setattr("scribe_bot.web.routes.accounts.test_key", lambda c, aid: calls.append(aid) or {"ok": True, "cost_usd": 0.00001})
    page = client.get("/settings/keys").text
    assert "Family Gemini" in page and "May train · demo only" in page and "Key stored" in page and "No key" in page
    assert "This will cost about" in client.post("/settings/accounts/1/test").text and calls == []
    assert "Key works (test cost $0.00001)." in client.post("/settings/accounts/1/test", data={"confirm": "1"}).text
    assert calls == [1]
    assert client.post("/settings/accounts/999/test").status_code == 404

def test_shared_audio_goes_into_the_recording_pipeline(env):
    client, conn, cfg = env
    r = client.post("/capture/share", data={"title": "Recording 12"}, files={"files": ("Recording 12.m4a", b"audio", "audio/mp4")})
    assert "shows up in Review" in r.text
    assert [p.name for p in (cfg.data_dir / "uploads").iterdir()] == ["Recording 12.m4a"]   # the folder ingest watches
    assert one(conn, "SELECT count(*) FROM capture WHERE kind = 'voice'") == 0               # not parked in Capture


def test_sharing_an_older_recording_puts_it_in_review(env, tmp_path):
    import hashlib
    client, conn, cfg = env
    audio = b"synthetic older recording"
    orig = tmp_path / "Recording 2.m4a"; orig.write_bytes(audio)
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                 "VALUES (50, 'recorder', ?, ?, 1026, '2026-10-19T18:58:35+00:00', 'backlog', 'Recording 2')",
                 (str(orig), hashlib.sha256(audio).hexdigest()))
    assert "Older recordings" in client.get("/review").text and 'action="/recordings/50/review"' in client.get("/review").text
    r = client.post("/capture/share", files={"files": ("Recording 2.m4a", audio, "audio/mp4")})
    assert "never processed" in r.text and "now in Review" in r.text
    assert one(conn, "SELECT status FROM recording WHERE id = 50") == "awaiting_approval"
    assert one(conn, "SELECT count(*) FROM review_item WHERE kind = 'approval' AND ref_id = 50 AND status = 'open'") == 1
    assert list((cfg.data_dir / "uploads").iterdir()) == []                       # our duplicate copy is removed
    assert 'id="older"' not in client.get("/review").text                          # it moved up to "waiting for your OK"
    again = client.post("/capture/share", files={"files": ("Recording 2.m4a", audio, "audio/mp4")})
    assert "already has this recording" in again.text                              # shared twice: said plainly
    assert client.post("/recordings/50/review").status_code == 404                  # no longer an older recording


def test_talk_time_follows_reassigned_lines_and_shows_on_person_page(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert "Alex Example</span> 50%" in page and "8 s · 2 turns" in page       # lines 0 and 2, 4 s each
    conn.execute("UPDATE segment SET speaker_id = 1 WHERE idx = 1")             # move Brian's line to Alex
    page = client.get("/conversations/1").text
    assert "Alex Example</span> 75%" in page and "12 s · 1 turn" in page
    person = client.get("/people/2").text
    assert 'id="talk-time"' in person and "1 conversation" in person and "Alex Example 75%, you 0%" in person
    assert 'id="talk-time"' not in client.get("/people/1").text                 # not on Brian's own page


def test_speaking_stats_and_tips_are_local_and_shown(env):
    client, conn, _ = env
    from scribe_bot.core import talk
    conn.execute("UPDATE segment SET text = 'Um, so, you know, um, I think we should, uh, ship it' WHERE idx = 1")
    s = talk.speaking(conn, 1)                                    # Brian's one 4-second line
    assert s["words"] == 12 and s["fillers"] == 4 and s["top_fillers"][0] == ("um", 2)
    assert any("filler words a minute" in t for t in talk.tips(s)) and any("words a minute" in t for t in talk.tips(s))
    page = client.get("/conversations/1").text
    assert 'id="sec-speaking"' in page and "at least 4" in page and "“um” 2" in page
    assert "You 180 · Alex Example" in page and "140–180" in page            # others' pace shown next to Brian's
    assert talk.tips({**s, "wpm": 185}) == talk.tips({**s, "wpm": 150})       # no slow-down tip until above 190
    assert any("slowing toward" in t for t in talk.tips({**s, "wpm": 195}))
    me = client.get("/people/1").text
    assert 'id="my-speaking"' in me and "Last 7 days" in me and "Early numbers" in me
    assert 'id="my-speaking"' not in client.get("/people/2").text and talk.speaking(conn, 999) is None


def test_needs_you_later_and_dismiss(env):
    client, conn, _ = env
    rid = conn.execute("SELECT id FROM review_item WHERE kind = 'glossary' AND status = 'open'").fetchone()[0]
    page = client.get("/").text
    assert f'action="/review/{rid}/later"' in page and f'action="/review/{rid}/dismiss"' in page and "Needs you · 3" in page
    client.post(f"/review/{rid}/later")
    page = client.get("/").text
    assert "Glossary suggestions" not in page and "Needs you · 2" in page and 'aria-label="2 need you"' in page
    snoozed = page.split('id="snoozed"')[1]                                    # still in view, under Snoozed
    assert "Zephyr" in snoozed and "Glossary suggestion" in snoozed and "back tomorrow 8:00 am" in snoozed
    assert f'action="/review/{rid}/now"' in snoozed
    assert "Glossary suggestions" not in client.get("/review").text                 # Review › Open = Needs you
    review = client.get("/review?tab=snoozed").text
    assert "Glossary suggestions" in review and "Snoozed · back tomorrow 8:00 am" in review and f'action="/review/{rid}/now"' in review
    client.post(f"/review/{rid}/now")
    assert "Glossary suggestions" in client.get("/").text
    client.post(f"/review/{rid}/dismiss")
    assert "Glossary suggestions" not in client.get("/review").text
    dismissed = client.get("/review?tab=dismissed").text                          # the log keeps it, with Restore
    assert "Zephyr" in dismissed and "Dismissed" in dismissed and f'action="/review/{rid}/restore"' in dismissed
    client.post(f"/review/{rid}/restore")
    assert "Glossary suggestions" in client.get("/").text and client.post(f"/review/{rid}/restore").status_code == 400
    assert client.post("/needs/nope/later").status_code == 404


def test_summary_templates_per_meeting_type(env):
    """F6: the meeting type adds its template to the prompt; 1:1 keeps the rated v2 prompt exactly."""
    client, conn, _ = env
    from scribe_bot.ai import extract, prompts
    base = prompts.extract_system("Brian")
    assert prompts.extract_system("Brian", "1:1") == base and "Meeting type" not in base
    conn.execute("UPDATE conversation SET type = '1:1' WHERE id = 1")
    auto = extract._prompt(conn, 1)[0]                  # type not picked by Brian: the AI may spot a Learning talk
    assert auto.startswith(base.split("\n- title")[0]) and '"learning": true' in auto
    page = client.get("/conversations/1").text
    assert 'action="/conversations/1/type"' in page and '<option value="multi"' in page
    client.post("/conversations/1/type", data={"type": "multi"})
    system = extract._prompt(conn, 1)[0]
    assert "Meeting type: Group." in system and "one short paragraph per main topic" in system
    assert '"learning": true' not in system             # Brian picked it: never overridden
    assert client.post("/conversations/1/type", data={"type": "nope"}).status_code == 400
    client.post("/settings/templates/multi", data={"text": "Keep it to {me}'s three bullet points."})
    assert "Keep it to Brian's three bullet points." in extract._prompt(conn, 1)[0]
    settings_page = client.get("/settings/templates").text
    assert 'id="templates"' in settings_page and "Edited" in settings_page
    client.post("/settings/templates/multi", data={"text": "", "reset": "1"})
    assert "one short paragraph per main topic" in extract._prompt(conn, 1)[0]
    conn.execute("UPDATE conversation SET summary = 'S', summary_type = '1:1', private = 0 WHERE id = 1")
    assert 'id="template-hint"' in client.get("/conversations/1").text          # made with 1:1, now Group: suggest a re-run


def test_area_toggle_applies_to_counts_everywhere(env):
    client, conn, _ = env
    work = client.get("/tasks?area=work").text
    assert 'Waiting on <span class="n">1</span>' in work and 'Inbox <span class="n">1</span>' in work
    home = client.get("/tasks?area=personal").text
    assert 'Waiting on <span class="n">0</span>' in home and 'Mine <span class="n">0</span>' in home
    assert 'Inbox <span class="n">1</span>' in home                              # not filed yet: shows under both
    assert "Send the budget" not in client.get("/people/2?area=personal").text
    assert "owes you" not in client.get("/people?area=personal").text


def test_today_badge_matches_needs_you_and_follows_the_toggle(env):
    """One number: the Today tab badge = "Needs you · N" = the rows there (review items, older recordings, old tasks)."""
    client, conn, _ = env
    t = db.now()
    conn.execute("INSERT INTO task (title, owner_person_id, status, created_at, updated_at, stale_at) "
                 "VALUES ('Old synthetic task', 1, 'open', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', ?)", (t,))
    conn.execute("INSERT INTO recording (source, original_path, sha256, received_at, status, title) "
                 "VALUES ('recorder', '/x/old.m4a', 'old1', ?, 'backlog', 'Older one')", (t,))
    page = client.get("/").text
    assert "Needs you · 5" in page and 'aria-label="5 need you"' in page        # speaker + inbox + glossary + older + old tasks
    home = client.get("/?area=personal").text                                    # conversation 1 is Work
    assert "Needs you · 3" in home and 'aria-label="3 need you"' in home and "Speakers to name" not in home
    assert "Glossary suggestions" in home                                        # no conversation: shows under both
    assert 'aria-label="3 need you"' in client.get("/tasks").text                # every page, same toggle (cookie)


def test_snooze_choices_and_lines(env):
    """Remind me: Brian picks when; the M14 and older-recordings lines snooze the same way and can come back now."""
    from datetime import datetime
    from scribe_bot.web.routes import snooze_options, snooze_until
    client, conn, _ = env
    mon = datetime(2026, 10, 19, 9, 30).astimezone()                          # a Monday morning, local time
    back = lambda choice, at="": datetime.fromisoformat(snooze_until(choice, at, now=mon)).astimezone()
    assert back("1h") == datetime(2026, 10, 19, 10, 30).astimezone()
    assert back("evening") == datetime(2026, 10, 19, 18, 0).astimezone()
    assert back("tomorrow") == datetime(2026, 10, 20, 8, 0).astimezone()
    assert back("monday") == datetime(2026, 10, 26, 8, 0).astimezone()        # a week on, not today
    assert back("pick", "2026-10-21T14:15") == datetime(2026, 10, 21, 14, 15).astimezone()
    assert [v for v, _ in snooze_options(mon)] == ["1h", "evening", "tomorrow", "monday"]
    assert "evening" not in [v for v, _ in snooze_options(datetime(2026, 10, 19, 17, 5).astimezone())]
    assert "Next Monday, Oct 26 (8 AM)" in dict(snooze_options(mon)).values()
    rid = one(conn, "SELECT id FROM review_item WHERE kind = 'glossary'")
    assert client.post(f"/review/{rid}/later", data={"until": "pick", "at": "2020-01-01T08:00"}).status_code == 400
    assert client.post(f"/review/{rid}/later", data={"until": "pick", "at": "soon"}).status_code == 400
    client.post(f"/review/{rid}/later", data={"until": "1h"})
    assert "Zephyr" in client.get("/").text.split('id="snoozed"')[1]
    conn.execute("INSERT INTO recording (source, original_path, sha256, received_at, status, title) "
                 "VALUES ('recorder', '/x/old.m4a', 'old1', ?, 'backlog', 'Older one')", (db.now(),))
    client.post("/needs/older/later", data={"until": "monday"})
    snoozed = client.get("/").text.split('id="snoozed"')[1]
    assert "1 older recording not processed yet" in snoozed and 'action="/needs/older/now"' in snoozed
    client.post("/needs/older/now")
    assert "1 older recording not processed yet" in client.get("/").text.split('id="needs-you"')[1].split('id="snoozed"')[0]
    assert client.post("/needs/nope/now").status_code == 404


def test_review_log_restores_what_was_dismissed(env):
    """Done and Dismissed keep everything Brian handled; Restore brings back a skipped recording or a removed Inbox task,
    and Undo of "Wrong, remove" reopens the Inbox item too."""
    client, conn, _ = env
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title) "
                 "VALUES (9, 'recorder', '/x/9.m4a', 'r9', ?, 'awaiting_approval', 'Oct 19 at 16_40')", (db.now(),))
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', 9, ?, ?)",
                 (json.dumps({"recording_id": 9, "title": "Oct 19 at 16_40", "minutes": 5, "ai_cost": 0.01}), db.now()))
    rid = one(conn, "SELECT id FROM review_item WHERE kind = 'approval'")
    client.post(f"/review/{rid}/dismiss")                                         # Skip
    assert one(conn, "SELECT status FROM recording WHERE id = 9") == "declined"
    log = client.get("/review?tab=dismissed").text
    assert "Recording waiting for your OK" in log and "Skipped" in log
    client.post(f"/review/{rid}/restore")
    assert one(conn, "SELECT status FROM recording WHERE id = 9") == "awaiting_approval"
    assert "Recordings waiting for your OK" in client.get("/").text
    client.post("/tasks/3/delete")                                               # the Inbox task: Wrong, remove
    inbox = one(conn, "SELECT id FROM review_item WHERE kind = 'inbox'")
    assert "Removed" in client.get("/review?tab=dismissed").text
    client.post(f"/review/{inbox}/restore")
    assert conn.execute("SELECT status, inbox FROM task WHERE id = 3").fetchone()[:] == ("open", 1)
    assert "Maybe book a venue" in client.get("/tasks?tab=inbox").text
    client.post("/tasks/3/delete")
    client.post(f"/undo/{one(conn, 'SELECT max(id) FROM undo_action')}")
    assert one(conn, f"SELECT status FROM review_item WHERE id = {inbox}") == "open"
    client.post("/speakers/3/name", data={"new_name": "Riley Test"})
    done = client.get("/review?tab=done").text
    assert "Speaker to name" in done and "Named" in done and 'href="/conversations/1"' in done


def test_unfiled_conversations_show_under_work_and_home(env):
    """Still being processed (no area yet): under both. Finished and unfiled: the Not sorted list instead (test below);
    Search still finds it under either, marked Not sorted."""
    client, conn, _ = env
    conn.execute("UPDATE conversation SET area = NULL, status = 'processing' WHERE id = 1")
    for area in ("work", "personal"):
        assert 'href="/conversations/1"' in client.get(f"/conversations?area={area}").text
    conn.execute("UPDATE conversation SET status = 'done' WHERE id = 1")
    for area in ("work", "personal"):
        page = client.get(f"/search?q=zephyr&area={area}").text
        assert 'href="/conversations/1">' in page and ">Not sorted</span>" in page
    conn.execute("UPDATE conversation SET area = 'work' WHERE id = 1")
    assert 'href="/conversations/1"' not in client.get("/conversations?area=personal").text


def test_today_spend_starts_at_local_midnight(env):
    from datetime import datetime, time, timezone
    from scribe_bot.web.routes import local_midnight
    assert datetime.fromisoformat(local_midnight()) == datetime.combine(date.today(), time()).astimezone(timezone.utc)


def test_waiting_on_quick_add_asks_who(env):
    client, conn, _ = env
    page = client.get("/tasks?tab=waiting").text.split('class="quick-add"')[1].split("</form>")[0]
    assert '<option value="">Who owes it?</option>' in page and "required" in page and ">Me<" not in page
    assert client.post("/tasks", data={"title": "Synthetic", "owner": ""}).status_code == 400
    assert client.post("/tasks", data={"title": "Synthetic", "owner": "999"}).status_code == 400
    client.post("/tasks", data={"title": "Send the synthetic slides", "owner": "2"})
    assert one(conn, "SELECT owner_person_id FROM task WHERE title = 'Send the synthetic slides'") == 2
    assert client.post("/people/2/edit", data={"name": "  "}).status_code == 400


def test_brians_own_row_counts_his_open_tasks(env):
    page = env[0].get("/people").text
    me = page.split('href="/people/1"')[1].split('href="/people/')[0]
    assert "2 open tasks" in me and "owes you" not in me                        # his own tasks, not "owes you"


def test_tasks_inbox_and_old_views_and_same_wording(env):
    client, conn, _ = env
    assert 'aria-label="Open or done"' not in client.get("/tasks?tab=inbox").text      # Done is always empty there
    assert 'aria-label="Open or done"' in client.get("/tasks?tab=mine").text
    old = client.get("/tasks?show=old").text
    assert "Old tasks to check" in old and 'class="on" aria-current=page' not in old.split('aria-label="Task lists"')[1].split("</nav>")[0]
    assert "Keep · Work" in client.get("/").text and "Keep · Work" in client.get("/tasks?tab=inbox").text


def test_status_page_keeps_the_page_frame(env):
    page = env[0].get("/status").text
    assert 'aria-label="3 need you"' in page and "checked " in page and "-" not in page.split("checked ")[1][:12]


def test_plain_names_not_internal_labels(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert "AI: Claude Sonnet" in page and "brian:claude" not in page
    conn.execute("UPDATE conversation SET processed_by = 'family:gemini-2.5-flash' WHERE id = 1")
    assert "Sent to Gemini" in client.get("/conversations/1").text
    from scribe_bot.web.routes import recorder_title
    assert recorder_title("Oct 19 at 16_40") == "Oct 19 at 16:40" and recorder_title("Intro call") == "Intro call"


def test_due_dates_read_like_words(env):
    from scribe_bot.web.app import day_label
    today = date.today()
    assert day_label(today.isoformat()) == "today" and day_label((today + timedelta(days=1)).isoformat()) == "tomorrow"
    assert day_label(YESTERDAY) == "yesterday" and day_label("2020-03-04") == "Mar 4, 2020" and day_label("someday") == "someday"
    assert "Overdue · due yesterday" in env[0].get("/tasks").text


def test_search_results_open_the_exact_line(env):
    client, conn, _ = env
    page = client.get("/search?q=budget").text
    assert 'class="item tap-row hit"' in page and 'href="/conversations/1#L2"' in page      # "I'll send the budget by Friday."
    assert 'href="/conversations/1#sec-summary"' in client.get("/search?q=plan").text


def test_search_groups_by_conversation_with_tabs_filters_and_one_conversation(env, monkeypatch):
    client, conn, _ = env
    page = client.get("/search?q=budget").text
    assert 'class="card result-group"' in page and 'href="/conversations/1">' in page and "in 1 conversation" in page
    assert ">Lines <span class=\"n\">" in page and ">People <span class=\"n\">0</span>" in page   # tab counts
    said = conn.execute("SELECT sp.person_id FROM segment g JOIN speaker sp ON sp.id = g.speaker_id "
                        "WHERE g.text LIKE '%budget%'").fetchone()[0]
    other = conn.execute("SELECT id FROM person WHERE id != ?", (said,)).fetchone()[0]
    assert 'href="/conversations/1#L2"' in client.get(f"/search?q=budget&said={said}").text
    assert 'href="/conversations/1#L2"' not in client.get(f"/search?q=budget&said={other}").text   # someone else said it? no
    assert 'href="/conversations/1#L2"' not in client.get("/search?q=budget&start=2999-01-01").text
    one = client.get("/search?q=budget&conv=1").text
    assert "← All results" in one and 'href="/conversations/1#L2"' in one
    from scribe_bot.web import routes
    monkeypatch.setattr(routes, "COMMON_HITS", 0)   # a word this common isn't grouped: newest hits, with a hint
    common = client.get("/search?q=budget").text
    assert "just the newest 100" in common and 'href="/conversations/1#L2"' in common and "result-group" not in common


def test_boxes_are_clickable_like_conversation_rows(env):
    """Every box that leads somewhere opens it with a tap anywhere on it (tap-row + one stretched link)."""
    client, conn, _ = env
    people = client.get("/people").text
    assert 'class="item tap-row"><div class="item-main"><a class="item-link" href="/people/2"' in people
    assert 'class="item tap-row"' in client.get("/help").text
    conn.execute("UPDATE task SET due = '2020-01-01' WHERE id = 2")                   # a late promise to Brian
    assert 'class="item-link" href="/conversations/1#L2">Send the budget' in client.get("/").text
    assert 'class="item-link" href="/conversations/1">Weekly sync with Alex' in client.get("/costs").text
    conn.execute("INSERT INTO person (id, name, created_at) VALUES (8, 'Alex Other', ?)", (db.now(),))
    assert 'class="item-link" href="/people/8/merge?into=2"' in client.get("/people").text


def test_tiles_reuse_the_m14_layout(env):
    """Speaking, person stats and Costs use the Status tile (name + Good/Watch/Problem + big value + hover details)."""
    client, conn, _ = env
    page = client.get("/conversations/1").text.split('id="sec-speaking"')[1]
    assert page.count('class="tile') >= 4 and "Pace" in page and 'class="light' in page
    person = client.get("/people/2").text
    assert 'id="talk-time"' in person and 'href="#person-open"' in person and "1 overdue" in person   # Review the launch deck
    costs = client.get("/costs").text
    assert "This month" in costs and "Good under 50%" in costs and 'class="light ok"' in costs



def test_costs_match_the_console(env):
    """Costs › Update from the Anthropic console: the month becomes the console's number; the Data Ledger stays outbound-only."""
    client, conn, _ = env
    assert "Credits left</span>" not in client.get("/costs").text.split("<form")[0]
    month = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM ledger_entry").fetchone()[0]
    page = client.post("/costs/console", data={"spent": f"${month + 0.15:.2f}", "credits": "4.80"}).text
    assert "Matched." in page and "Credits left" in page and "$4.80" in page and "Matched to the Anthropic console" in page
    assert "Matched to the Anthropic console" not in client.get("/settings/privacy").text
    assert "Type the dollar amounts" in client.post("/costs/console", data={"spent": "lots"}).text

def test_older_recordings_are_test_meetings(env, tmp_path):
    client, conn, _ = env
    orig = tmp_path / "Oct 2 at 10_15.m4a"; orig.write_bytes(b"synthetic older audio")
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                 "VALUES (60, 'recorder', ?, 'old60', 600, ?, 'backlog', 'Oct 2 at 10_15')", (str(orig), db.now()))
    page = client.get("/review").text
    assert "Use as a new recording" in page and "Oct 2 at 10:15" in page and "test meeting" in page
    assert "Use as a new recording" in client.get("/help").text
    client.post("/recordings/60/review")
    assert one(conn, "SELECT status FROM recording WHERE id = 60") == "awaiting_approval"
    assert "Recordings waiting for your OK" in client.get("/").text                 # like one that just arrived


def test_history_is_one_tap_from_today_and_explained_in_help(env):
    client = env[0]
    assert 'href="/review?tab=done"' in client.get("/").text
    help_page = client.get("/help").text
    assert "Snoozed" in help_page and "Trash (at the bottom of Conversations)" in help_page and "Restore" in help_page


def test_search_history_lives_in_search(env):
    client, conn, _ = env
    client.get("/search?q=budget")
    client.get("/search?q=Budget")                      # same search, any case: one entry, moved to the top
    client.get("/search?q=plan")
    page = client.get("/search").text
    assert page.index("plan</a>") < page.index("Budget</a>") and page.count("budget</a>") + page.count("Budget</a>") == 1
    assert "<datalist" not in page and "Turn off history" in page and 'action="/search/history/clear"' in page
    after = client.get("/search?q=budget").text
    assert 'class="icon-btn search-clear" href="/search"' in after                    # ✕ in the box clears the search
    assert 'data-q="plan"' in after and "data-recent" in after                         # recent ones drop down under the box
    assert client.get("/search/history", follow_redirects=False).headers["location"] == "/search"   # no separate page
    client.post("/search/history/remove", data={"q": "plan"})
    assert "plan</a>" not in client.get("/search").text
    client.post("/search/history/keep", data={"on": "off"})
    client.get("/search?q=friday")
    assert conn.execute("SELECT count(*) FROM search_history").fetchone()[0] == 0   # off clears and stops remembering
    off = client.get("/search").text
    assert "Search history is off" in off and "Turn on history" in off and "data-recent" not in client.get("/search?q=x").text
    client.post("/search/history/keep", data={"on": "on"})
    for i in range(12):
        client.get(f"/search?q=word{i}")
    assert "Show more · 2 left" in client.get("/search").text                         # 10 at first, the rest one tap away
    client.post("/search/history/clear")
    assert conn.execute("SELECT count(*) FROM search_history").fetchone()[0] == 0


def test_unsorted_conversations_have_their_own_list_not_both(env):
    client, conn, _ = env
    conn.execute("UPDATE conversation SET area = NULL, status = 'done' WHERE id = 1")
    work = client.get("/conversations?area=work").text
    assert 'href="/conversations/1"' not in work and "Not sorted · 1" in work      # not under Work (or Home) any more
    conn.execute("UPDATE conversation SET area_guess = 'personal' WHERE id = 1")   # the AI wasn't sure: its guess comes first
    lst = client.get("/conversations?unsorted=1").text
    assert 'href="/conversations/1"' in lst and 'action="/conversations/1/area"' in lst and 'btn-primary">Home</button>' in lst
    assert "AI guess: <b>Home</b>" in lst
    client.post("/conversations/1/area", data={"area": "personal"})
    assert "Not sorted" not in client.get("/conversations?area=personal").text
    assert 'href="/conversations/1"' in client.get("/conversations?area=personal").text


def test_today_has_your_week_tiles_and_speaking_is_a_page(env):
    client, conn, _ = env
    page = client.get("/").text
    assert 'id="your-week"' in page and 'href="/speaking"' in page and 'href="/tasks?show=done"' in page
    assert "Your speaking this week →" not in page                       # the small link it replaces
    assert "<h1>Your speaking</h1>" in client.get("/speaking").text


def test_more_menu_is_short_and_everything_else_has_a_home(env):
    client, conn, _ = env
    menu = client.get("/tasks").text.split('class="menu-panel">')[1].split("</details>")[0]
    assert [h for h in ("/status", "/costs", "/review#older", "/help") if f'href="{h}"' in menu] == ["/status", "/costs", "/help"]
    assert "Dark mode" not in menu and "menu-label" not in menu
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status) "
                 "VALUES (61, 'recorder', '/x61', 'sha61', ?, 'backlog')", (db.now(),))
    menu = client.get("/tasks").text.split('class="menu-panel">')[1].split("</details>")[0]
    assert 'href="/review#older"' in menu and ">1</span>" in menu                       # only while there are some
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = 1", (db.now(),))
    talks = client.get("/conversations").text
    assert 'href="/review#older"' in talks and 'href="/conversations?trash=1"' in talks   # under the list
    today = client.get("/").text
    assert 'href="/speaking"' in today and 'href="/review?tab=done"' in today
    assert 'href="/tasks?tab=decisions"' in client.get("/tasks").text
    assert 'id="ledger"' in client.get("/settings/privacy").text and 'data-theme-set="dark"' in client.get("/settings/appearance").text


def test_bell_shows_new_notifications_and_clears_when_opened(env):
    client, conn, _ = env
    conn.execute("INSERT INTO notification (channel, title, status, created_at) VALUES ('bot', 'Ready: Synthetic call', 'sent', ?)",
                 (db.now(),))
    assert 'aria-label="Notifications, 1 new"' in client.get("/tasks").text
    page = client.get("/notifications").text
    assert "Ready: Synthetic call" in page and ">New</span>" in page
    assert 'aria-label="Notifications"' in client.get("/tasks").text            # dot gone once opened
    assert "Ready: Synthetic call" not in client.get("/settings").text           # the list moved out of Settings


def test_notifications_dismiss_restore_undo_and_send_a_test(env):
    client, conn, _ = env
    for t in ("Ready: Synthetic call", "Morning briefing: synthetic"):
        conn.execute("INSERT INTO notification (channel, title, status, created_at) VALUES ('bot', ?, 'sent', ?)", (t, db.now()))
    first = one(conn, "SELECT id FROM notification WHERE title = 'Ready: Synthetic call'")
    client.post(f"/notifications/{first}/dismiss")
    page = client.get("/notifications").text
    assert "Ready: Synthetic call" not in page and "Morning briefing: synthetic" in page and 'Dismissed <span class="n">1</span>' in page
    assert "Ready: Synthetic call" in client.get("/notifications?tab=dismissed").text and ">Restore</button>" in client.get(
        "/notifications?tab=dismissed").text
    client.post(f"/notifications/{first}/restore")
    assert one(conn, "SELECT count(*) FROM notification WHERE dismissed_at IS NULL") == 2
    client.post("/notifications/dismiss-all")
    assert "All caught up" in client.get("/notifications").text
    client.post(f"/undo/{one(conn, 'SELECT max(id) FROM undo_action')}")              # Undo brings them all back
    assert one(conn, "SELECT count(*) FROM notification WHERE dismissed_at IS NULL") == 2
    r = client.post("/notifications/test", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/notifications?sent=1"
    assert [tuple(row) for row in conn.execute("SELECT channel, status FROM notification WHERE title LIKE '🔔 Test%' ORDER BY id")] \
        == [("bot", "queued"), ("alerts", "queued")]                                 # one to each Discord channel
    assert "Test sent to the queue" in client.get("/notifications?sent=1").text


def test_status_charts_fold_under_their_title_and_tiles_open_them(env):
    client, conn, _ = env
    from datetime import datetime, timedelta, timezone
    for i in range(3):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5 * i)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO machine_sample (ts, cpu_pct) VALUES (?, ?)", (ts, 20 + i))
    page = client.get("/status").text
    assert '<details class="card chart-card chart" id="chart-cpu_pct"' in page and "now 20%" in page
    assert '<span class="chart-tip" hidden></span>' in page                      # the pointed-at value, themed, in the title row


def test_people_filter_sort_and_show_more(env):
    client, conn, _ = env
    for i in range(60):
        conn.execute("INSERT INTO person (name, created_at) VALUES (?, ?)", (f"Zed{i:02d} Example", db.now()))
    page = client.get("/people").text
    assert "Show more · " in page and 'name="q"' in page
    found = client.get("/people?q=Zed07").text
    assert "Zed07 Example" in found and "Zed08 Example" not in found
    by_name = client.get("/people?sort=name&n=200").text
    assert by_name.index("Zed01 Example") < by_name.index("Zed02 Example")
    assert "Nobody matches" in client.get("/people?q=zzzz").text


def test_late_promise_has_a_copyable_nudge_that_is_never_sent(env):
    client, conn, _ = env
    conn.execute("UPDATE task SET due = '2020-01-03' WHERE id = 2")                   # Alex's "Send the budget" is late
    page = client.get("/").text
    assert 'data-copy="#nudge-2"' in page
    assert "Hi Alex, quick check-in on “Send the budget”. We&#39;d said by Friday, January 3. Any update? Thanks!" in page
    assert one(conn, "SELECT count(*) FROM notification") == 0                       # copying is local; nothing queued to send


def test_task_reminder_waits_in_snoozed_then_shows_under_needs_you(env):
    client, conn, _ = env
    client.post("/tasks/1/remind", data={"until": "1h"})
    page = client.get("/").text
    assert "Task reminder" in page and 'action="/tasks/1/remind/now"' in page          # waiting in Snoozed
    before = int(page.split('class="dot"')[1].split(">")[1].split("<")[0]) if 'class="dot"' in page else 0
    client.post("/tasks/1/remind/now")
    page = client.get("/").text
    assert ">Reminders<" in page and 'action="/tasks/1/remind/clear"' in page          # now under Needs you
    after = int(page.split('class="dot"')[1].split(">")[1].split("<")[0])
    assert after == before + 1                                                         # and counted in the Today badge
    client.post("/tasks/1/remind/clear")
    assert ">Reminders<" not in client.get("/").text
    assert client.post("/tasks/999/remind", data={"until": "1h"}).status_code == 404


def test_new_badge_until_a_finished_conversation_is_opened(env):
    client, conn, _ = env
    conn.execute("UPDATE conversation SET opened_at = NULL, status = 'done' WHERE id = 1")
    assert 'class="badge badge-new">New</span>' in client.get("/conversations").text
    client.get("/conversations/1")
    assert 'badge-new' not in client.get("/conversations").text


def test_delete_person_and_undo(env):
    """Delete: lines become unnamed, voiceprint goes, their tasks go to the Inbox; Undo puts everything back."""
    client, conn, _ = env
    conn.execute("INSERT INTO voiceprint (person_id, embedding, sample_count, updated_at) VALUES (2, ?, 3, ?)",
                 (bytes(range(16)), db.now()))
    conn.execute("UPDATE decision SET people = '[2]' WHERE id = 1")
    assert "Delete…" in client.get("/people/2").text and "Delete…" not in client.get("/people/1").text
    client.post("/people/2/delete")
    assert conn.execute("SELECT count(*) FROM person WHERE id = 2").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM voiceprint").fetchone()[0] == 0
    assert conn.execute("SELECT person_id FROM speaker WHERE id = 1").fetchone()[0] is None
    t = conn.execute("SELECT owner_person_id, inbox FROM task WHERE id = 2").fetchone()
    assert tuple(t) == (1, 1)
    assert conn.execute("SELECT people FROM decision WHERE id = 1").fetchone()[0] == "[]"
    assert client.get("/conversations/1").status_code == 200 and client.get("/tasks?tab=inbox").status_code == 200
    uid = conn.execute("SELECT max(id) FROM undo_action").fetchone()[0]
    client.post(f"/undo/{uid}")
    assert conn.execute("SELECT name FROM person WHERE id = 2").fetchone()[0] == "Alex Example"
    assert conn.execute("SELECT embedding, sample_count FROM voiceprint WHERE person_id = 2").fetchone()[:] == (bytes(range(16)), 3)
    assert conn.execute("SELECT person_id FROM speaker WHERE id = 1").fetchone()[0] == 2
    assert tuple(conn.execute("SELECT owner_person_id, inbox FROM task WHERE id = 2").fetchone()) == (2, 0)
    assert conn.execute("SELECT people FROM decision WHERE id = 1").fetchone()[0] == "[2]"
    assert client.post("/people/1/delete").status_code == 400


def test_undo_delete_person_after_their_id_was_reused(env):
    """Person ids are reused: if someone new took the deleted person's id, Undo brings them back under a new id
    (their lines, tasks, decisions and voiceprint follow) and leaves the newcomer alone."""
    client, conn, _ = env
    conn.execute("INSERT INTO voiceprint (person_id, embedding, sample_count, updated_at) VALUES (2, ?, 3, ?)",
                 (bytes(range(16)), db.now()))
    conn.execute("UPDATE decision SET people = '[2]' WHERE id = 1")
    client.post("/people/2/delete")
    uid = conn.execute("SELECT max(id) FROM undo_action").fetchone()[0]
    conn.execute("INSERT INTO person (id, name, created_at) VALUES (2, 'Newcomer Example', ?)", (db.now(),))   # gets the freed id
    newcomer = "Newcomer Example"
    assert client.post(f"/undo/{uid}", follow_redirects=False).status_code == 303
    back = conn.execute("SELECT id FROM person WHERE name = 'Alex Example'").fetchone()[0]
    assert back != 2 and conn.execute("SELECT name FROM person WHERE id = 2").fetchone()[0] == newcomer
    assert conn.execute("SELECT person_id FROM speaker WHERE id = 1").fetchone()[0] == back
    assert tuple(conn.execute("SELECT owner_person_id, inbox FROM task WHERE id = 2").fetchone()) == (back, 0)
    assert conn.execute("SELECT people FROM decision WHERE id = 1").fetchone()[0] == f"[{back}]"
    assert conn.execute("SELECT person_id FROM voiceprint").fetchone()[0] == back
    assert client.get(f"/people/{back}").status_code == 200


def test_unnamed_speaker_placeholder_names_and_renames(env, monkeypatch):
    """A task the AI gives "Speaker 3" goes to a placeholder for this meeting's speaker 3, not a shared "Speaker 3".
    Renaming the placeholder names the speaker and fixes the meeting's text."""
    from scribe_bot.ai import extract
    client, conn, _ = env
    monkeypatch.setattr("scribe_bot.web.routes.identify.enroll",
                        lambda c, sid, pid, cfg=None: c.execute("UPDATE speaker SET person_id = ?, confirmed = 1 WHERE id = ?", (pid, sid)))
    conv = conn.execute("SELECT * FROM conversation WHERE id = 1").fetchone()
    ph = extract._person_id(conn, "Speaker 3", "work", conv=conv)
    assert extract._person_id(conn, "speaker 3", "work", conv=conv) == ph
    assert extract._person_id(conn, "Speaker 1", "work", conv=conv) == 2           # a named speaker: their person
    name = conn.execute("SELECT name FROM person WHERE id = ?", (ph,)).fetchone()[0]
    assert name.startswith("Speaker 3 from Weekly sync with Alex on ")
    conn.execute("UPDATE task SET owner_person_id = ?, title = 'Speaker 3 sends notes' WHERE id = 2", (ph,))
    client.post(f"/people/{ph}/edit", data={"name": "Jordan Example", "area": "work"})
    assert conn.execute("SELECT person_id FROM speaker WHERE id = 3").fetchone()[0] == ph
    assert conn.execute("SELECT stands_for_speaker FROM person WHERE id = ?", (ph,)).fetchone()[0] is None
    assert conn.execute("SELECT title FROM task WHERE id = 2").fetchone()[0] == "Jordan Example sends notes"


def test_placeholder_renamed_to_someone_known_moves_the_page_there(env, monkeypatch):
    """Edit autosaves in the background (hx-swap none), so a plain redirect was swallowed: the page stayed on a person
    that the merge had just removed, and every later change failed with "No such person"."""
    from scribe_bot.ai import extract
    client, conn, _ = env
    monkeypatch.setattr("scribe_bot.web.routes.identify.enroll", lambda c, sid, pid, cfg=None: None)
    ph = extract._person_id(conn, "Speaker 3", "work", conv=conn.execute("SELECT * FROM conversation WHERE id = 1").fetchone())
    r = client.post(f"/people/{ph}/edit", data={"name": "Alex Example", "area": "work"}, headers={"HX-Request": "true"},
                    follow_redirects=False)
    assert r.status_code == 200 and r.headers["hx-redirect"] == "/people/2"
    assert one(conn, "SELECT count(*) FROM person WHERE id = ?", ph) == 0                  # merged into Alex
    ph = extract._person_id(conn, "Speaker 3", "work", conv=conn.execute("SELECT * FROM conversation WHERE id = 1").fetchone())
    r = client.post(f"/people/{ph}/edit", data={"name": "Alex Example", "area": "work"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/people/2"                    # a plain form post still redirects


def test_naming_the_speaker_folds_in_the_placeholder(env, monkeypatch):
    from scribe_bot.ai import extract
    client, conn, _ = env
    monkeypatch.setattr("scribe_bot.web.routes.identify.enroll", lambda c, sid, pid, cfg=None: None)
    ph = extract._person_id(conn, "Speaker 3", "work", conv=conn.execute("SELECT * FROM conversation WHERE id = 1").fetchone())
    conn.execute("UPDATE task SET owner_person_id = ? WHERE id = 2", (ph,))
    client.post("/speakers/3/name", data={"person_id": 2})
    assert conn.execute("SELECT count(*) FROM person WHERE id = ?", (ph,)).fetchone()[0] == 0
    assert conn.execute("SELECT owner_person_id FROM task WHERE id = 2").fetchone()[0] == 2


def test_conversations_sort(env):
    """Conversations › Sort: Newest (default), Oldest, Longest, A–Z; an unknown value falls back to Newest."""
    client, conn, _ = env
    t = db.now()
    conn.execute("INSERT INTO conversation (id, title, started_at, duration_s, area, status, created_at, updated_at) "
                 "VALUES (40, 'Apple tasting', '2020-01-01T10:00:00+00:00', 7200, 'work', 'done', ?, ?)", (t, t))

    def apple_first(o):
        page = client.get(f"/conversations?order={o}&area=all").text
        return page.index("Apple tasting") < page.index("Weekly sync")
    assert not apple_first("newest") and apple_first("oldest") and apple_first("longest") and apple_first("title")
    assert not apple_first("bogus")
    assert '<option value="longest" selected>Longest</option>' in client.get("/conversations?order=longest").text


def test_rename_updates_earlier_ai_text(env):
    """Renaming a person swaps the old name in earlier summaries, key points, tasks and decisions (no AI call)."""
    client, conn, _ = env
    conn.execute("UPDATE conversation SET summary = 'Alex Example and Alex-Team met; Alex will send the budget.', "
                 "takeaways = '[\"Alex owns the budget\"]' WHERE id = 1")
    conn.execute("UPDATE task SET title = 'Alex sends the budget' WHERE id = 2")
    client.post("/people/2/edit", data={"name": "Sam Rivera", "area": "work"})
    c = conn.execute("SELECT summary, takeaways FROM conversation WHERE id = 1").fetchone()
    assert c["summary"] == "Sam Rivera and Alex-Team met; Sam will send the budget."
    assert "Sam owns the budget" in c["takeaways"]
    client.post("/people/2/edit", data={"name": "Sam Rivera Jr", "area": "work"})          # adding to the name: text unchanged
    assert conn.execute("SELECT summary FROM conversation WHERE id = 1").fetchone()[0].startswith("Sam Rivera and")
    assert conn.execute("SELECT title FROM task WHERE id = 2").fetchone()[0] == "Sam sends the budget"
    assert conn.execute("SELECT count(*) FROM search_index WHERE search_index MATCH 'Sam' AND kind = 'task'").fetchone()[0] == 1


def test_unsure_task_looks_right(env):
    client, conn, _ = env
    conn.execute("UPDATE task SET inbox = 0 WHERE id = 3")
    assert "/tasks/3/sure" in client.get("/tasks").text
    client.post("/tasks/3/sure")
    assert tuple(conn.execute("SELECT confidence, inbox FROM task WHERE id = 3").fetchone()) == (1, 0)
    assert "/tasks/3/sure" not in client.get("/tasks").text


def test_weak_voice_match_asks_to_confirm_the_name(env):
    client, conn, _ = env
    conn.execute("UPDATE review_item SET payload = json_set(coalesce(payload, '{}'), '$.guess_id', 2, '$.score', 0.79) "
                 "WHERE kind = 'speaker'")
    page = client.get("/review").text
    assert "voice match 79%" in page and '<option value="2" selected>' in page and ">Confirm</button>" in page


def test_person_page_has_a_jump_bar(env):
    client, _, _ = env
    page = client.get("/people/2").text
    assert 'class="jump-bar"' in page and 'href="#person-facts"' in page and 'id="sec-convs"' in page


def test_short_speaker_can_be_folded_into_another(env):
    client, conn, _ = env
    a, b = [r[0] for r in conn.execute("SELECT id FROM speaker WHERE conversation_id = 1 ORDER BY id LIMIT 2")]
    conn.execute("UPDATE speaker SET talk_seconds = 5 WHERE id = ?", (b,))
    conn.execute("UPDATE segment SET speaker_id = ?, start_s = 0, end_s = 5 WHERE id = "
                 "(SELECT min(id) FROM segment WHERE conversation_id = 1)", (b,))
    conn.execute("UPDATE segment SET speaker_id = ? WHERE conversation_id = 1 AND id > "
                 "(SELECT min(id) FROM segment WHERE conversation_id = 1)", (a,))
    page = client.get("/conversations/1").text
    assert f'action="/speakers/{b}/fold"' in page and "probably part of someone else" in page
    client.post(f"/speakers/{b}/fold", data={"into": a})
    assert conn.execute("SELECT count(*) FROM segment WHERE speaker_id = ?", (b,)).fetchone()[0] == 0
    assert client.post(f"/speakers/{a}/fold", data={"into": a}).status_code == 400


def test_jump_bars_list_every_section_and_learning_hides_tasks(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert "data-jump-top" in page and 'href="#processing"' in page and 'id="sec-mine"' in page
    conn.execute("UPDATE conversation SET type = 'learning' WHERE id = 1")
    page = client.get("/conversations/1").text
    assert 'id="sec-mine"' not in page and 'id="sec-decisions"' not in page and 'id="sec-facts"' in page
    assert 'class="jump-bar"' in client.get("/people/1").text                      # Brian's own page too


def test_color_per_speaker_in_one_conversation(env):
    client, conn, _ = env
    sid = conn.execute("SELECT id FROM speaker WHERE conversation_id = 1 AND person_id IS NULL").fetchone()[0]  # unnamed too
    assert f'action="/speakers/{sid}/color"' in client.get("/conversations/1").text
    client.post(f"/speakers/{sid}/color", data={"color": "#dc2626"})
    assert "background:#dc2626" in client.get("/conversations/1").text
    client.post(f"/speakers/{sid}/color", data={"color": "javascript:x"})          # only the 8 palette colors
    assert conn.execute("SELECT color FROM speaker WHERE id = ?", (sid,)).fetchone()[0] is None


def test_notifications_dismiss_themselves_when_done(env):
    client, conn, _ = env
    from scribe_bot.discord_bot import notify
    old = "2026-01-01T00:00:00+00:00"
    for title, link in (("New recording waiting for your OK · 3 min", "/review"), ("Ready: x", "/conversations/1"),
                        ("☀️ Mon Jan 1: 1 overdue", "/")):
        conn.execute("INSERT INTO notification (channel, title, link, created_at) VALUES ('bot', ?, ?, ?)", (title, link, old))
    conn.execute("UPDATE review_item SET status = 'done' WHERE kind = 'approval'")
    conn.execute("UPDATE conversation SET status = 'done' WHERE id = 1")
    client.get("/conversations/1")                                                  # opening it handles its Ready ping
    left = [r[0] for r in conn.execute("SELECT title FROM notification WHERE created_at = ? AND dismissed_at IS NULL", (old,))]
    assert left == [] and notify.tidy(conn) == 0


def test_markdown_exports(env):
    client, conn, _ = env
    r = client.get("/conversations/1/export.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"] and "## Transcript" in r.text
    assert "## Transcript" not in client.get("/conversations/1/export.md?transcript=0").text
    lst = client.get("/conversations/export.md").text
    assert lst.startswith("# Scribe Bot export · ") and "## Summary" in lst
    assert client.get("/people/2/export.md").text.startswith("# ")
    page = client.get("/conversations/1").text
    assert 'href="/conversations/1/export.md" download hx-boost="false"' in page and 'href="/conversations/1/export.pdf"' in page
    for url in ("/conversations/1/export.pdf", "/people/2/export.pdf", "/conversations/export.pdf?type=1:1"):
        r = client.get(url)
        assert r.status_code == 200 and r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"], url
    assert client.get("/conversations/1/export.exe").status_code == 404
    assert 'href="/conversations/export.pdf?' in client.get("/conversations?type=1:1").text


def test_markdown_transcript_one_line_per_paragraph(env, monkeypatch):
    """Brian 2026-10-22: in a Markdown viewer the transcript ran together as one paragraph. Each line is now its own
    paragraph (time and name first); the PDF keeps the lines tight as before."""
    client, _, _ = env
    md = client.get("/conversations/1/export.md").text
    part = md.split("## Transcript\n\n", 1)[1]
    assert part.startswith("[0:00] **Alex Example:** Hi Brian, thanks for making time.\n\n[0:04] **")
    turns = [ln for ln in part.split("\n") if ln]
    assert len(turns) == 4 and all(ln.startswith("[") for ln in turns)
    assert "\n\n".join(turns) + "\n" == part   # a blank line between every two lines, none missing
    from fpdf import FPDF

    from scribe_bot.web import export
    gaps, ln = [], FPDF.ln
    monkeypatch.setattr(FPDF, "ln", lambda self, h=None: (gaps.append(h), ln(self, h))[1])
    export.to_pdf("## Transcript\n\n[0:00] **A:** hi\n\n[0:04] **B:** yo\n\n[61:02] **A:** ok\n", "t")
    assert gaps.count(2) == 2   # after the heading and at the end, never between transcript lines


@pytest.mark.parametrize("q", ["person=&type=&start=&end=&order=newest", "start=2026-10-21", "start=2026-10-21&end=",
                               "person=2&type=learning&start=2026-10-01&end=2026-10-22&order=oldest", "end=2026-10-21",
                               "type=&order=", "person=&sort=", "start=2026-10-22&end=2026-10-01"])
def test_every_filter_combination_works(env, q):
    client, _, _ = env
    for page in ("/conversations", "/people", "/tasks", "/search?q=zephyr&", "/conversations/export.md"):
        url = page + ("" if page.endswith("&") else "?") + q
        assert client.get(url).status_code == 200, url

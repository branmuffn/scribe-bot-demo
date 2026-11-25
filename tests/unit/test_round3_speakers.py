"""Round 3 (Brian, 2026-11-25): B8 titles, B9 "Don't know" first, B10/B12 one "N to name" count, B11 "...or the rest",
B5 tap a color. Synthetic data only."""
import re
from pathlib import Path

from scribe_bot.core.names import first_name
from test_web_pages import env, one  # noqa: F401  (pytest fixture)

H = {"sec-fetch-site": "same-origin"}


def test_first_name_skips_titles():
    assert first_name("Dr. Nina Okafor") == "Nina"
    assert first_name("Dr Okafor") == "Dr Okafor" and first_name("Dr. Okafor") == "Dr. Okafor"
    assert first_name("Prof. Dr. Jane Q. Doe") == "Jane"
    assert first_name("Alex Rivera") == "Alex" and first_name("Alex") == "Alex" and first_name("") == ""
    assert first_name("Mrs. Lee") == "Mrs. Lee" and first_name("Coach Sam Torres") == "Sam"


def test_talk_bar_shows_first_name_not_title(env):
    client, conn, _ = env
    conn.execute("UPDATE person SET name = 'Dr. Nina Example' WHERE id = 2")
    page = client.get("/conversations/1").text
    bar = page.split('class="talk sp-bar"')[1].split("</button>")[0]
    assert ">Nina " in bar and ">Dr. " not in bar
    person = client.get("/people/2").text
    assert "You owe Nina</span>" in person and "You owe Dr.</span>" not in person


def test_two_doctors_are_not_suggested_as_the_same_person(env):
    _, conn, _ = env
    from scribe_bot.core import people
    conn.execute("INSERT INTO person (name, area, created_at) VALUES ('Dr. Amy One', 'both', '2026-01-01')")
    conn.execute("INSERT INTO person (name, area, created_at) VALUES ('Dr. Ben Two', 'both', '2026-01-01')")
    assert not [p for p in people.duplicate_suggestions(conn) if "Dr." in p["from_name"] and "Dr." in p["into_name"]]


def test_to_name_count_is_the_same_in_the_list_and_on_the_page(env):
    """B12: the list said 3, the page said 2. A speaker whose lines all moved away (stored talk time still 25 s)
    and a short new one (stored talk time 0) used to be counted differently."""
    client, conn, _ = env
    conn.execute("INSERT INTO speaker (id, conversation_id, cluster, talk_seconds) VALUES (40, 1, 7, 25)")   # no lines left
    conn.execute("INSERT INTO speaker (id, conversation_id, cluster, talk_seconds) VALUES (41, 1, 8, 0)")    # lines, stale time
    conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) VALUES (1, 9, 30, 34, 41, 'Hello there.')")
    page = client.get("/conversations/1").text
    n_page = int(re.search(r"(\d+) to name</button>", page).group(1))
    lst = client.get("/conversations").text.split('id="conv-list"')[1]
    n_list = int(re.search(r'class="cr-name"[^>]*>(\d+) to name', lst).group(1))
    assert n_page == n_list == 2   # speaker 3 and the new speaker 41


def test_dont_know_this_one_or_the_rest(env):
    client, conn, _ = env
    conn.execute("INSERT INTO speaker (id, conversation_id, cluster, talk_seconds) VALUES (41, 1, 8, 40)")
    conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) VALUES (1, 9, 30, 70, 41, 'Hello there.')")
    page = client.get("/conversations/1").text
    assert 'value="__idk_rest">Don\'t know this one or the other 1<' in page
    client.post("/speakers/3/name", data={"person_id": "", "special": "idk_rest"}, headers=H)
    assert one(conn, "SELECT count(*) FROM speaker WHERE conversation_id = 1 AND person_id IS NULL AND unknown = 0") == 0


def test_colors_open_their_speaker_and_unnamed_are_marked(env):
    client, _, _ = env
    page = client.get("/conversations/1").text
    bar = page.split('class="talk sp-bar"')[1].split("</button>")[0]
    assert 'class="sp-seg sp-seg-open" data-sp-id="3"' in bar and '? Speaker' in bar      # still to name: striped, "?"
    assert 'class="sp-seg" data-sp-id="1"' in bar
    assert '<details data-sp-id="3" class="speaker-chip sp-unnamed' in page and "data-sp-first-open" in page
    ui = Path("scribe_bot/web/static/ui.js").read_text()
    assert '.speaker-chip[data-sp-id="' in ui and "sbRowChanged" in ui   # B5 and B10 wiring


def test_picker_puts_dont_know_first():
    js = Path("scribe_bot/web/static/app.js").read_text()
    assert "q ? h + ex : (ex ? '<div class=\"pp-extras\">' + ex + '</div>' : \"\") + h" in js

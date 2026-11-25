"""Claude Duet round 4 (Brian, 2026-11-25): fixes from his testing of round 3, each with the same-kind sweep."""
import re
from pathlib import Path

from test_web_pages import env  # noqa: F401

STATIC = Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static"
UI, APP, CSS = ((STATIC / f).read_text() for f in ("ui.js", "app.js", "app.css"))


def test_r4_02_reading_pane_only_on_list_pages(env):
    """A talk's "You owe" list has task rows too, so the pane opened over the talk ("Pick a task to see it here")."""
    client, _, _ = env
    assert 'data-nav="today"' in client.get("/").text
    assert 'data-nav="conversations"' in client.get("/conversations/1").text
    assert "var LISTS = /^(today|tasks|inbox)$/;" in UI
    assert 'LISTS.test(document.body ? document.body.dataset.nav || "" : "")' in UI


def test_r4_04_pane_switch_drops_the_pop_up_nudge():
    """Switching Pop up → Right/Below kept the pop-up's sideways nudge and height cap: the pane opened mid-screen."""
    assert "function unfit(p)" in APP and "if (inPane(d)) { [].forEach.call(d.querySelectorAll(POP), unfit); return; }" in APP
    assert "window.ScribeRefit = function ()" in APP
    assert "split(); refit();" in UI and "paint(); refit();" in UI
    # the pane check reads the layout itself, not the class that paint() sets a moment later
    assert "window.ScribeDp = { on: function () { return !!document.body && on(); }" in UI


def test_r4_07_undo_bar_centred_on_the_list_column():
    """The undo bar was centred on the whole page and cut off under the reading pane ("Moved", Talks, Right)."""
    assert "left: calc(var(--side) + (100vw - var(--side) - var(--ub-r)) / 2)" in CSS
    for rule in ('body.pane-on .undo-bar { --ub-r: var(--pane-w, 46vw) }', 'body.dp-on .undo-bar { --ub-r: var(--dpw) }',
                 'body.dp-on .undo-bar { --ub-r: calc(var(--dpbw) + 32px)'):
        assert rule in CSS


def test_r4_06_bars_stay_above_rows():
    """A row with an open menu or sheet (z-index 20; inbox rows 60) scrolled over the search row and top bar."""
    lifted = max(int(z) for z in re.findall(r"\.item:has\(details\[open\]\)[^{]*\{[^}]*z-index: (\d+)", CSS))
    assert lifted == 20 and ".lbar { z-index: 21 }" in CSS and ".dtop { z-index: 44 }" in CSS
    assert "z-index: auto !important" in CSS.split("R4-06 + R4-07")[1].split("R4-07: the undo")[0]


def test_r4_08_touch_press_never_grows_the_row():
    """The pressed tint's 10px ring made a held or swiped row look bigger on a phone; touch keeps the tint inside."""
    block = CSS.split("R4-08 (Brian")[1].split("R4-06 + R4-07")[0]
    assert "@media (hover: none)" in block and "box-shadow: none !important" in block and ".task-row" in block


def test_r4_03_task_last_line_uses_the_width(env):
    """The date · reminder · talk line was cut with "…" although the space under "Stop reminding" was empty."""
    block = CSS.split("R4-03 (Brian")[1]
    assert ".task-row:has(> .item-actions > form .btn) > .item-actions { position: absolute" in block
    assert "> .item-main > :is(.item-title, .item-full) { margin-right: 150px }" in block


def test_r4_01_connection_warmed_after_a_network_change():
    assert 'addEventListener("online", ping);' in APP and 'document.addEventListener("visibilitychange", ping);' in APP


def test_r4_05_layout_switch_everywhere(env):
    """Today's layout switch (12.53.17) also sits in the empty reading pane and the talk/person preview bar."""
    assert "function addEverywhere()" in UI and '"Full page"' in UI and ".rpane-bar .dp-layout" in CSS


def test_r4_09_line_links_send_the_transcript_with_the_page(env):
    """A line link (#L22) loaded the page, then the transcript, then scrolled: slow. ?tx=1 sends it with the page."""
    client, _, _ = env
    assert 'location.pathname + "?tx=1#" + line.id' in UI
    page = client.get("/conversations/1?tx=1")
    assert page.status_code == 200 and 'id="L0"' in page.text


def test_r4_11_13_line_fixes_compact_with_who_said_this(env):
    """Who said this is in the ⋯ panel too; picking a name saves it; the panel is one row per fix."""
    client, _, _ = env
    html = client.get("/conversations/1?tx=1").text
    assert "data-le-who" in html and "Someone new…" in html and "le-panel" in html
    assert 'select[data-le-who]' in APP and "requestSubmit" in APP


def test_r4_12_fixes_pause_audio_and_stop_auto_scroll():
    assert '.lines details.line-edit[open], .lines [contenteditable=\\"true\\"], .fix-menu' in APP


def test_r4_14_less_is_now_hide(env):
    """"Less" → "Hide" (Brian, 2026-11-25)."""
    client, _, _ = env
    assert ">Less<" not in client.get("/").text
    assert 'more.textContent = "Less"' not in UI and 'more.textContent = "Hide"' in UI


def test_r4_15_16_tags_save_without_a_page_load(env):
    """Adding and removing a tag reloaded the whole talk; the browser's own tag list looked unformatted (13.17.52)."""
    client, _, _ = env
    html = client.get("/conversations/1").text
    assert 'class="row tag-form" novalidate' in html and "data-tag-input" in html and 'autocomplete="off"' in html
    # the form is read before the box is cleared (it once sent an empty name)
    assert UI.index("var body = new URLSearchParams(new FormData(f)).toString();") < UI.index('inp.value = ""; shut();')
    assert ".tag-sugg" in CSS and ".tag-pending" in CSS


def test_r4_17_drop_downs_look_like_the_right_click_menu():
    """Desktop drop-downs open the app's own menu (pointer: fine); phones keep the native list, which he likes."""
    assert "sel-menu" in UI and "(pointer: fine)" in UI and "data-native" in UI
    assert ".sel-menu" in CSS
    # the person picker keeps its own searchable sheet (B9's "Don't know" at the top lives there)
    assert """s.hasAttribute("data-people") || s.querySelector('option[value="__new"]')""" in UI


def test_r4_18_speaker_panel_compact(env):
    """The speaker panel (13.21.59): turns in the summary, a Voice button beside "Who is it?" instead of a full-width
    player that read 0:00 / 0:00, smaller colour dots. Same button on Review and People."""
    client, _, _ = env
    html = client.get("/conversations/1").text
    assert "sp-turns" not in html and "<audio controls" not in html
    for path in ("/conversations/1", "/review", "/people"):
        assert "<audio controls preload=\"none\" src=\"/audio/speaker/" not in client.get(path).text
    assert "[data-voice]" in APP and 'id === "player"' in APP and ".voice-btn" in CSS and ".sp-pick" in CSS


def test_r4_07_sweep_other_fixed_notes_centre_on_the_list():
    """Same bug type as the undo bar: the Copied note and the tour card were centred on the whole page."""
    assert ".copy-note, .tour { --ub-r: 0px; left: calc(var(--side) + (100vw - var(--side) - var(--ub-r)) / 2) }" in CSS

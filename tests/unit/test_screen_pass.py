"""Screen pass (2026-11-26): phone and computer, light and dark, on the public demo's made-up data (F51–F69)."""
import re
from pathlib import Path

from tests.unit.test_public_demo import demo  # noqa: F401  (the fixture)

WEB = Path(__file__).resolve().parents[2] / "scribe_bot" / "web"


def _static(name: str) -> str:
    return (WEB / "static" / name).read_text()


def _tpl(name: str) -> str:
    return (WEB / "templates" / name).read_text()


def test_f51_the_red_plus_steps_aside_while_a_sheet_is_open():
    """F51: the ＋ button was drawn over Talks › Export and Filter (it hid ".pdf" and the dates)."""
    assert "html.pop-lock .fab { visibility: hidden }" in _static("app.css")


def test_f52_remind_me_in_a_sheet_opens_in_place():
    """F52: Inbox sheet › Remind me opened upward inside the sheet's scroll box and was cut off."""
    css = _static("app.css")
    assert ".fx-body .chip-menu[open] { flex-basis: 100% }" in css
    assert re.search(r"\.fx-body \.chip-menu\[open\] \.chip-pop \{ position: static;", css)


def test_f53_remind_me_list_does_not_repeat_its_own_name():
    """F53: "Remind me" row › "REMIND ME" label right under it; only the icon-only clock menu keeps the label."""
    assert ".ts-in > .menu-label, .chip-pop > .menu-label { display: none }" in _static("app.css")


def test_f54_unsure_titles_are_ink_not_link_blue():
    assert "a.item-title.plain { color: var(--ink) }" in _static("app.css")


def test_f55_inbox_sheet_does_not_repeat_the_name(demo):
    """F55: with no quick title the line under the sheet's name started with the same name again."""
    html = demo.get("/inbox").text
    pairs = re.findall(r'<div class="sh-head"><b>(.*?)</b>.*?<div class="meta">(.*?)</div>', html, re.S)
    assert pairs
    for name, meta in pairs:
        assert not meta.strip().startswith(name + " ·"), (name, meta)


def test_f56_inbox_sheet_chips_use_drawn_icons(demo):
    html = demo.get("/inbox").text
    assert "＋ Transcript" not in html and "⏱" not in html
    assert '<use href="#i-plus"/></svg> Transcript</button>' in html
    assert '<use href="#i-calendar"/></svg> Process at…</button>' in html


def test_f57_actions_are_named_like_their_buttons(demo):
    """F57: "AI notes / Transcript only / Transcribe + AI" for the actions the buttons call Summarize and Transcribe."""
    inbox = demo.get("/inbox").text
    assert "▶ AI notes ·" not in inbox and "Summarize · $" in inbox
    help_page = demo.get("/help").text
    assert "AI notes or Transcript only" not in help_page and "Transcribe + AI" not in help_page
    assert "Summarize or Transcribe" in help_page and "<b>Transcribe</b>, <b>Merge</b>" in help_page
    assert "Open the Inbox to choose Summarize or Transcribe." in _tpl("_review.html")
    assert "or <i>Transcribe</i> (free, stays on the M14)" in _tpl("today.html")
    tx = _tpl("inbox_transcript.html")
    assert '"Summarize", "rc-"' in tx and "Transcribe (no AI)</button>" in tx and "Transcript Only (No AI)</button>" not in tx
    assert "Transcribe + AI" not in _tpl("costs.html")
    assert "Set to {{ 'summarize' if x.payload.process_ai else 'transcribe' }}" in _tpl("inbox.html")


def test_f58_fixed_times_say_am(demo):
    help_page = demo.get("/help").text
    assert "1 am" not in help_page and "Resume at 1 AM" in help_page and "Tonight, 1 AM" in help_page
    assert "Resume at 1 am" not in _tpl("macros.html")


def test_f59_talks_select_bar_says_selected(demo):
    html = demo.get("/conversations").text
    assert '<span class="pick-n" aria-live="polite">0 selected</span>' in html and "picked talks" not in html
    js = _static("app.js")
    assert 'textContent = n() + " selected"' in js and '" picked"' not in js and "picked talk" not in js


def test_f60_select_bars_dim_their_actions_at_zero():
    """F60: Inbox, People, Tasks and Trash kept their actions looking ready with nothing selected (Talks dims them)."""
    js = _static("app.js")
    assert 'b.querySelectorAll(".pk-bot button").forEach(function (x) { x.disabled = !n; });' in js
    assert 'b.querySelectorAll(".pk-bot button").forEach(function (x) { x.disabled = !k; });' in js
    assert ".pk-bot button:disabled { opacity: .5 }" in _static("app.css")


def test_f61_person_menu_puts_delete_last(demo):
    html = demo.get("/people/2").text
    menu = html[html.index('data-panel="pa-merge"'):]
    assert menu.index("Export as PDF") < menu.index("Delete…")


def test_f62_durations_say_min(demo):
    html = demo.get("/people?sort=talked").text
    talked = re.findall(r"talked ([^<]+)</span>", html)
    assert talked and all(t.endswith(("min", " s", " h")) for t in talked), talked
    person = demo.get("/people/2").text
    m = re.search(r'id="talk-time".*?<span class="r1-v">([^<]+)</span>', person, re.S)
    if m:
        assert re.fullmatch(r"\d+ (s|min)|\d+ h( \d+ min)?", m.group(1)), m.group(1)
    for t in ("person.html", "people.html", "macros.html"):
        assert "|hours" not in _tpl(t)


def test_f63_no_fillers_says_none_heard():
    assert "{% if s.fillers %}at least {{ s.fillers }}{% else %}none heard{% endif %}" in _tpl("macros.html")


def test_f64_demo_talk_shows_no_zero_cost_and_estimates_round_like_the_inbox(demo):
    html = demo.get("/conversations/1").text
    assert "$0.000" not in html
    assert re.search(r"Re-run AI · ~\$\d+\.\d{2}<|Summarize · ~\$\d+\.\d{2}<|~\$0\.00\d<", html)


def test_f65_links_that_go_on_use_the_chevron(demo):
    assert "Talks ›</a>" in demo.get("/speaking").text
    assert "in this conversation →" not in _tpl("search.html") and "in this conversation ›</a>" in _tpl("search.html")


def test_f66_today_due_tasks_get_the_people_list(demo):
    """F66: a Due task's sheet on Today had no Due or Who rows and no title editing; it works like on Tasks."""
    html = demo.get("/").text
    due = html.split('id="sec-due"', 1)[1].split('</section>', 1)[0]
    assert 'class="ts-title" data-edit' in due or ('class="ts-title"' in due and "data-edit-tap" in due), "title not editable"
    assert 'class="ts-row ts-pick" data-quick-pick' in due


def test_f67_add_task_sheet_title_fits_whose(demo):
    html = demo.get("/tasks").text
    assert "<b>Add a task</b>" in html and "Add a task for me" not in html


def test_f68_reading_pane_follows_theme_density_and_notes_at_once():
    """F68: Quick settings › Dark left the open reading pane light until it reloaded (same for density and notes)."""
    app_js, ui_js = _static("app.js"), _static("ui.js")
    assert 'f.contentDocument && f.contentDocument.documentElement' in app_js and 'd.setAttribute("data-theme", t)' in app_js
    assert "fd.dataset.density = d.dataset.densitySet" in ui_js and "fd.dataset.pnotes = pn.dataset.pnotesSet" in ui_js


def test_f69_settings_says_what_no_reading_pane_does(demo):
    html = demo.get("/settings/appearance").text
    assert "None: talks and people open as a full page, recordings and tasks in a pop-up." in html

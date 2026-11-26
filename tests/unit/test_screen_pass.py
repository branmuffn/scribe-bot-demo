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
    assert "in this conversation →" not in _tpl("search.html") and "in this talk ›</a>" in _tpl("search.html")


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


def test_pages_say_talk_not_conversation(demo):
    """F86 (500 pass): F2 made "Talks" the word everywhere, but "conversation" was left on Your speaking ("6 conversations
    · 1 min", which also read as six talks lasting a minute in all), People, Search, a talk in Trash, the AI ledger and
    the confirm headings. (Settings › Templates is left out: its text is the AI's own instructions, which Brian edits.)"""
    import re
    pages = ["/", "/inbox", "/conversations", "/conversations/1", "/people", "/people/2", "/tasks", "/review", "/speaking",
             "/search?q=budget", "/help", "/costs", "/status", "/processing", "/cleanup", "/more", "/settings",
             "/people/1", "/conversations?trash=1", "/notifications", "/decisions", "/search/history", "/capture"]
    for path in pages + [f"/settings/{g}" for g in ("appearance", "ai", "keys", "privacy", "glossary", "backups")]:
        page = demo.get(path).text
        said = " ".join(re.findall(r'(?:aria-label|title|placeholder|alt)="([^"]*)"', page))   # what screen readers and tooltips say
        text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page, flags=re.S) + " " + said
        assert not re.search(r"\b[Cc]onversations?\b", text), (path, re.findall(r".{40}\b[Cc]onversations?\b.{20}", text)[:3])
    tour = _static("app.js")   # the demo tour's cards
    assert "Each talk opens with this strip" in tour and "one card per talk" in tour and "per conversation" not in tour


def test_costs_and_ledger_use_plain_words(demo):
    """F87 (500 pass): Costs and the Data Ledger showed the code's words: task "extract", status "ok" and "blocked",
    "$0.000" and "$0.0000" for a call that was never sent, and token counts without commas."""
    costs = demo.get("/costs").text
    ledger = demo.get("/settings/privacy").text
    for page in (costs, ledger):
        assert "$0.000" not in page and ">extract" not in page and "extract:" not in page
    assert 'data-label="Status">Sent<' in costs and 'data-label="Status">Blocked, not sent<' in costs
    assert 'data-label="Status">ok<' not in costs and re.search(r'data-label="Tokens" class="num">\d{1,3},\d{3} / ', costs)
    assert "AI notes: " in ledger and ">Blocked, not sent</span>" in ledger


def test_help_points_to_more_and_uses_the_button_names(demo):
    """F89 (500 pass): Help sent you to "⋯ at the top › Processing history" and listed "More (⋯)", but More lives in
    ☰ (phone) and the side bar (computer); the Inbox line and the tour still said AI notes and Transcript only."""
    html = demo.get("/help").text
    assert "⋯ at the top" not in html and "More (⋯)" not in html
    assert "More › Processing history (☰ on a phone, the side bar on a computer)" in html
    assert "AI notes, Transcript only" not in html and "Summarize, Transcribe or Delete" in html
    assert "Transcript only is free" not in _static("app.js") and "Transcribe is free and stays on the M14" in _static("app.js")


def test_ai_settings_list_note_fits_a_model_with_no_key(demo):
    """F90 (500 pass): "Only models with a connected key are listed" sat under Claude Sonnet marked "key missing", and
    the names read "Claude or Gemini or ChatGPT"."""
    html = demo.get("/settings/ai").text
    assert "Only models with a connected key are listed" not in html and " or Gemini or " not in html
    assert "Other models show up here once their key is connected." in html
    from scribe_bot.web.routes import templates
    f = templates.env.filters["or_list"]
    assert (f(["A"]), f(["A", "B"]), f(["A", "B", "C"])) == ("A", "A or B", "A, B or C")


def test_keys_page_says_may_train_once(demo):
    """F91 (500 pass): the Gemini free account showed the "May train · demo only" badge and then "· may train"."""
    html = demo.get("/settings/keys").text
    row = html[html.index("Gemini free"):][:1200]
    assert "May train · demo only" in row and "<span>may train</span>" not in row


def test_fold_away_sections_point_right_when_closed_and_down_when_open():
    """F92 (500 pass): Backups, the Data Ledger and Costs › Every call drew the › chevron turned up while closed and
    pointing right while open (the CSS was written for a down chevron)."""
    css = _static("app.css")
    assert ".fold-sec[open] > summary .chev .icon { transform: rotate(90deg) }" in css
    assert "transition: transform .18s; transform: rotate(-90deg)" not in css
    assert '<symbol id="i-chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/>' in _tpl("icons.html")   # it points right


def test_backups_say_ok_like_the_schedules(demo):
    """F93 (500 pass): backups said "ok" right above schedules saying "OK"."""
    html = demo.get("/settings/backups").text
    backups = html[html.index('id="backups"'):html.index("Schedules")]
    assert ">ok<" not in backups and ">OK<" in backups


def test_glossary_term_box_fills_its_row(demo):
    """F94 (500 pass): on a phone the "Correct term" box wrapped onto its own line but stopped two thirds across."""
    html = demo.get("/settings/glossary").text
    assert re.search(r'<input name="term"[^>]*class="grow"', html)


def test_an_open_template_does_not_repeat_its_first_line(demo):
    """F95 (500 pass): opening Group showed its cut-off first line and then the same text again in the edit box."""
    html = demo.get("/settings/templates").text
    assert 'class="item-sub tpl-peek">This is a group meeting.' in html
    assert "details.template[open] .tpl-peek { display: none }" in _static("app.css")


def test_no_link_jumps_to_an_empty_line(demo):
    """F96 (500 dark pass): Today's due tasks linked to "/conversations/7?tx=1#L": the page left the line number out
    and the templates only checked for None, so a missing one printed nothing. Every link to a transcript line now
    needs a number, and Today reads the line like Tasks does."""
    pages = ["/", "/tasks", "/tasks?tab=waiting", "/tasks?tab=inbox", "/tasks?tab=decisions", "/review", "/people/2",
             "/people/3", "/conversations/1", "/search?q=budget", "/notifications", "/inbox"]
    for path in pages:
        assert not re.search(r'#L["&]', demo.get(path).text), path
    due = demo.get("/").text.split('id="sec-due"', 1)[1].split("</section>", 1)[0]
    assert re.search(r'\?tx=1#L\d+"', due)
    for t in ("macros.html", "today.html", "_review.html", "search.html"):
        assert "line is not none" not in _tpl(t)


def test_trash_and_not_sorted_have_their_own_heading(demo):
    """F97 (500 pass): Trash showed "‹ Talks", then a "Talks" heading, a small TRASH label and the ＋ for a new
    recording; Not sorted did the same. Each now has its own heading with the count under it."""
    trash = demo.get("/conversations?trash=1").text
    assert "<h1>Trash</h1>" in trash and "<h1>Talks</h1>" not in trash and 'class="fab"' not in trash
    assert re.search(r'<div class="meta">\d+ items? · each is removed for good', trash)
    unsorted = demo.get("/conversations?unsorted=1").text
    assert "<h1>Not sorted</h1>" in unsorted and "<h1>Talks</h1>" not in unsorted
    talks = demo.get("/conversations").text
    assert "<h1>Talks</h1>" in talks and 'class="fab"' in talks


def test_merging_shows_what_each_person_brings(demo):
    """F98 (500 pass): Merge 2 people showed counts only on the second name ("4 talks, 0 tasks, 1 note", though the
    People list said "you owe 2"), so the first name had none and ticking another left them on the wrong row. Every
    row now has its own counts, in the People list's words, and the single merge says whose tasks move."""
    page = demo.get("/people/merge-many?ids=2,3").text
    assert page.count('class="meta pick-sub"') == 2
    assert "tasks," not in page and " 0 " not in re.sub(r"<[^>]+>", " ", page.split("<form", 1)[1].split("</form>", 1)[0])
    assert re.search(r'pick-sub">\d+ talks?', page)
    me = demo.get("/people/merge-many?ids=1,2").text
    assert me.count('class="meta pick-sub"') == 2 and "owes you" not in me.split("pick-sub", 2)[1]
    assert "they owe you," in _tpl("merge.html")


def test_a_speaker_name_opens_on_the_first_tap_and_its_menu_matches(demo):
    """F99 (500 pass): with a line's ⋯ fix panel open, tapping a speaker's name only closed the panel; the name
    needed a second tap. F100: its "Who said this line?" menu was drawn inverted (white in Dark, black in Light),
    unlike every other menu."""
    js = _static("app.js")
    closer = js.split('var KINDS = "details.speaker-chip', 1)[1].split("})();", 1)[0]
    assert 'if (t.closest(".lines .who, .who-pin-btn, [data-open-sheet]")) return;' in closer
    assert ".split-bar.fix-menu { background: var(--surface); color: var(--ink); border: 1px solid var(--line) }" in _static("app.css")


def test_the_reading_pane_clears_new_says_its_layout_and_fits_the_people_bar(demo):
    """1440 pass: a talk opened in the pane loses its New badge (F101), the pane's Layout buttons say which one is on
    from the start (F102), the empty People pane talks about people (F103), the People bar fits beside the pane (F104)."""
    ui = _static("ui.js")
    assert 'row.querySelectorAll(".badge-new").forEach(function (x) { x.remove(); });' in ui
    assert 'b.setAttribute("aria-pressed", html.dataset.pane === k[0]);' in ui
    assert 'x.setAttribute("aria-pressed", on); });' in ui
    assert '"Pick a recording to see it here." : "Pick a task to see it here."' in ui
    people = demo.get("/people", headers={"accept": "text/html"}).text
    assert "Pick a person to see them here." in people and "to read it here" not in people
    talks = demo.get("/conversations", headers={"accept": "text/html"}).text
    assert "Pick a talk to read it here." in talks
    assert "@media (min-width: 960px) { .people-tools .lb-q { flex-basis: 140px } }" in _static("app.css")


def test_an_empty_grow_spacer_has_no_minimum_width(demo):
    """F105 (1024 pass): the reading pane's bar is "Open full page", an empty .grow spacer, Layout and ✕. The spacer kept
    .grow's 10rem minimum, so in a ~310px pane Layout and ✕ sat past the right edge. A talk's bar (Talks, spacer,
    Summarize, ⋯) and the tour's buttons have the same spacer."""
    css = _static("app.css")
    assert ".grow { flex: 1; min-width: 10rem }" in css and ".grow:empty { min-width: 0 }" in css
    assert css.index(".grow:empty { min-width: 0 }") > css.index(".grow { flex: 1; min-width: 10rem }")
    for name in ("base.html", "conversation.html"):
        assert '<span class="grow"></span>' in _tpl(name)


def test_no_line_comment_swallows_code():
    """F102 slip: a comment added at the end of a line ran on into the next statement, so `g.appendChild(b)` became
    part of the comment and the Layout buttons were never built. No trailing // comment may hold a statement."""
    import glob
    code = re.compile(r'(\w\.\w+\s*=\s*[^=].*;|\w\([^)]*\);)')
    found = []
    for f in sorted(glob.glob(str(WEB / "static" / "*.js"))):
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            m = re.search(r"\s{2,}//\s(.*)$", line)
            if m and code.search(m.group(1)):
                found.append(f"{f.rsplit('/', 1)[1]}:{i}")
    assert found == []


def test_esc_closes_the_pane_unless_a_pop_up_is_open():
    """F106 (1024 pass): Esc closed the reading pane only when no <details> at all was open, so with the sidebar's
    More places open it never closed. F107: Esc pressed inside the pane closes it too. F108: on a short screen with
    More open, Add recording was squashed from 60 to 38 px."""
    ui = _static("ui.js")
    assert 'document.querySelector("details[open], dialog[open], .picking")' not in ui
    pops = re.search(r"var POPS = \[([^\]]*)\]", ui).group(1)
    kinds = re.findall(r'"([\w-]+)"', pops)
    for k in ("row-menu", "fx-sheet", "edit", "speaker-chip", "filters"):
        assert k in kinds
    for k in ("side-more", "fold-sec", "q-more", "section"):   # folds that stay open are not pop-ups
        assert k not in kinds
    assert "parent.postMessage({ sbPaneEsc: 1 }, location.origin)" in ui and "e.data.sbPaneEsc) { popOpen = false; escClose(); }" in ui
    assert "closePane(); if (at) at.focus();" in ui
    assert ".sidebar > * { flex-shrink: 0 }" in _static("app.css")


def test_the_empty_pane_shows_the_layout_switch_not_a_dead_close():
    """F109 (1024 pass): with nothing open, the Talks/People pane showed a ✕ that did nothing. It shows the Layout switch
    instead, like the empty pane on Inbox, Tasks and Today."""
    css = _static("app.css")
    assert ".rpane.empty .rpane-bar > :not(.grow):not(.dp-layout) { display: none }" in css
    assert ":not(.grow):not([data-rpane-close]) { display: none }" not in css


def test_j_and_k_load_the_transcript_and_carry_on_from_the_marked_line():
    """F110 (1024 pass): the transcript loads when its section opens, so before that j/k did nothing. F111: after a
    "line 6" link, k jumped back to line 1, and e couldn't fix the line the link marked."""
    js = _static("app.js")
    assert '(e.key === "j" || e.key === "k") && (lines().length || txLater())' in js
    assert "if (!ls.length) { pend = k; return; }" in js
    assert 'addEventListener("htmx:afterSettle", function () { if (pend && lines().length)' in js
    assert 'var mk = document.querySelector(".line.cursor");' in js
    assert 'e.key === "e" && document.querySelector(".line.cursor")' in js
    assert "cursor >= 0 && lines()[cursor]" not in js


def test_copy_says_it_copied_and_keeps_the_buttons_icon():
    """F112 (1024 pass): "c" copies the summary through a button inside the shut ⋯ menu, so "Copied ✓" showed nowhere.
    F113: putting the label back as text dropped the button's icon, and a second tap kept "Copied ✓" for good."""
    js = _static("app.js")
    assert "var t = copy.textContent;" not in js
    assert "copy.innerHTML = copy.dataset.was" in js and "if (!copy.dataset.was) copy.dataset.was = copy.innerHTML;" in js
    assert '!copy.getClientRects().length) ScribeToast(' in js
    assert "writeText(text.trim()).then(done, function () {" in js
    assert "window.ScribeToast = note;" in _static("ui.js")


def test_esc_with_a_menu_open_closes_only_the_menu_not_the_pane():
    # F114: app.js loads first and shuts Quick settings on Esc before ui.js looked, so the pane closed too
    ui = _static("ui.js")
    assert 'window.addEventListener("keydown", function (e) { if (e.key === "Escape") popOpen = !!document.querySelector(POPS); }, true);' in ui
    assert "|| popOpen || document.querySelector(POPS)) return;" in ui
    assert "{ popOpen = false; escClose(); }" in ui   # Esc inside the pane isn't held back by an old note
    assert ".ctx-menu" in ui.split("var POPS =")[1].split("var popOpen")[0]   # a right-click menu holds the pane too
    base = (Path(__file__).parents[2] / "scribe_bot/web/templates/base.html").read_text()
    assert base.index("/static/app.js") < base.index("/static/ui.js")   # why the note is needed

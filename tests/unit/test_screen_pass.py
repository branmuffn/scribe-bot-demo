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
        assert re.fullmatch(r"\d+ talks?, (\d+ (s|min)|\d+ h( \d+ min)?)", m.group(1)), m.group(1)
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


def test_a_line_link_moves_the_one_k_mark():
    """F117 (live check): press k (line 1 marked), then a task's "line 6" link: line 6 lit up but line 1 kept its mark,
    so the next k went to line 2. Every place that marks a line now clears the old mark first, and the address
    changing (back/forward) marks its line too."""
    js = _static("app.js")
    assert 'document.querySelectorAll(".line.cursor").forEach(function (l) { if (l !== ln) l.classList.remove("cursor"); });' in js
    assert js.count('if (l !== ln) l.classList.remove("cursor")') == 2          # markLine and the lines-arrived handler
    assert 'window.addEventListener("hashchange", function () {' in js
    assert 'markLine(document.getElementById(lnk.getAttribute("href").slice(1)))' in js   # same link twice: no hashchange
    assert "if (line) { openTranscript(); markLine(line); }" in js
    # nothing else adds the mark without clearing the old one
    assert js.count('classList.add("cursor")') == 3                            # markLine, the lines-arrived handler, j/k
    assert 'ls.forEach(function (l) { l.classList.remove("cursor"); });' in js


def test_the_linked_line_drops_its_tint_once_k_moves_on():
    """F119 (live check): the #L5 tint stayed on line 6 after k marked line 7, so two lines looked marked."""
    css = _static("app.css")
    assert ".lines:has(.line.cursor) .line:target:not(.cursor) { background: none; box-shadow: none }" in css
    assert css.index(".line:target {") < css.index(".line:target:not(.cursor)")   # later rule wins


def test_copy_says_it_copied_and_keeps_the_buttons_icon():
    """F112 (1024 pass): "c" copies the summary through a button inside the shut ⋯ menu, so "Copied ✓" showed nowhere.
    F113: putting the label back as text dropped the button's icon, and a second tap kept "Copied ✓" for good."""
    js = _static("app.js")
    assert "var t = copy.textContent;" not in js
    assert "copy.innerHTML = copy.dataset.was" in js and "if (!copy.dataset.was) copy.dataset.was = copy.innerHTML;" in js
    # F116 (live check): Chrome keeps boxes for a button inside a shut <details>, so getClientRects() alone never fired the note
    assert 'var unseen = !copy.getClientRects().length || !!copy.closest("details:not([open])")' in js
    assert "if (window.ScribeToast && unseen) ScribeToast(" in js
    assert "!copy.getClientRects().length) ScribeToast(" not in js
    macros = _tpl("macros.html")
    menu = macros[macros.index("{% macro menu("):]
    assert menu.index("<details") < menu.index("{{ caller() }}")   # the Copy summary button lives in a <details> menu
    assert 'data-copy="#recap"' in _tpl("conversation.html").split('{% call m.menu("this talk") %}')[1].split("{% endcall %}")[0]
    assert "writeText(text.trim()).then(done, function () {" in js
    assert "window.ScribeToast = note;" in _static("ui.js")


def test_esc_with_a_menu_open_closes_only_the_menu_not_the_pane():
    # F114: app.js loads first and shuts Quick settings on Esc before ui.js looked, so the pane closed too
    ui = _static("ui.js")
    assert 'window.addEventListener("keydown", function (e) { if (e.key === "Escape") popOpen = !!document.querySelector(POPS) || typing(); }, true);' in ui
    assert "|| popOpen || document.querySelector(POPS)) return;" in ui
    assert "{ popOpen = false; escClose(); }" in ui   # Esc inside the pane isn't held back by an old note
    assert ".ctx-menu" in ui.split("var POPS =")[1].split("var popOpen")[0]   # a right-click menu holds the pane too
    base = (Path(__file__).parents[2] / "scribe_bot/web/templates/base.html").read_text()
    assert base.index("/static/app.js") < base.index("/static/ui.js")   # why the note is needed


def test_esc_while_typing_in_a_list_box_leaves_the_pane_open():
    # F115: Esc in the top search (after "/") closed the reading pane; inside the pane a box already held it back
    ui = _static("ui.js")
    assert "function typing() { var a = document.activeElement;" in ui
    assert "|| typing(); }, true);" in ui
    assert 'if (e.key !== "Escape" || document.querySelector(POPS) || typing()) return;' in ui   # same rule inside the pane


def test_every_shared_macro_is_used():
    """Tidiness pass (2026-11-26): est_choices, type_badge and person_picker were left over from older screens. A macro
    nothing calls is dead weight in every page's template load."""
    import re
    src = _tpl("macros.html")
    pages = "".join(p.read_text() for p in (WEB / "templates").rglob("*.html"))
    py = "".join(p.read_text() for p in (WEB.parent).rglob("*.py"))
    unused = [n for n in re.findall(r"\{%-? macro (\w+)\(", src)
              if len(re.findall(rf"\b{n}\(", pages)) <= 1 and not re.search(rf"\b{n}\b", py)]
    assert unused == []


def _css_rules(css: str):
    """Every (at-rule context, selector, body) in app.css, comments removed."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, stack, buf = [], [], ""
    for ch in css:
        if ch == "{":
            stack.append(buf.strip()); buf = ""
        elif ch == "}":
            if buf.strip() and stack:
                rules.append((" ".join(stack[:-1]), stack[-1], buf.strip()))
            if stack:
                stack.pop()
            buf = ""
        else:
            buf += ch
    return rules


def _lum(h: str) -> float:
    h = h.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h[:6]
    r, g, b = (int(h[k:k + 2], 16) / 255 for k in (0, 2, 4))
    return .2126 * r + .7152 * g + .0722 * b


def test_no_fixed_colour_turns_unreadable_in_dark():
    """F120: a few labels kept light-mode hex colours, so in Dark they were dark on dark (the School badge, the
    Delete note button, the overnight card's icon) or ignored the theme (the Talks area dots). Any dark text
    colour or pale background written as a bare hex needs a dark-theme override for the same selector."""
    rules = _css_rules(_static("app.css"))
    dark = set()
    for ctx, sel, _ in rules:
        if "dark" in ctx + sel:
            for s in sel.split(","):
                dark.add(re.sub(r'^:root(\[data-theme="dark"\])?(:not\(\[data-theme="light"\]\))?\s*', "", s.strip()))
    bad = []
    for ctx, sel, body in rules:
        if "dark" in ctx + sel or "print" in ctx or sel.startswith((":root", "@")):
            continue
        for prop, val in re.findall(r"(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)", body):
            if "color-mix(" in val and "var(--" in val:
                continue                                                # mixed with a theme colour: follows the theme
            val = re.sub(r"var\([^()]*\)", "", val)                    # a var(--x, #hex) fallback follows the theme
            for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", val):
                risky = (prop == "color" and _lum(h) < .45) or (prop in ("background", "background-color") and _lum(h) > .6)
                if risky and not any(s.strip() in dark for s in sel.split(",")):
                    bad.append(f"{sel} {{ {prop}: {h} }}")
    assert not bad, bad


def test_what_me_means_shows_on_a_phone_too(demo):
    """F121 (500 dark pass): Settings › Summary templates explained "{me}" only in the section help, which phones
    hide, so on a phone the text box showed "{me}" with no word on what it is. Each template now says it under its box."""
    page = demo.get("/settings/templates").text
    notes = re.findall(r'<p class="meta" id="tpl-note-([\w-]+)">\{me\} means you\.', page)
    boxes = re.findall(r'aria-describedby="tpl-note-([\w-]+)"', page)
    assert notes and notes == boxes                                   # one note per template, tied to its box
    assert "{me} means you" not in re.sub(r'<p class="meta" id="tpl-note-.*?</p>', "", page)   # said once each, not twice


def test_every_schedule_reads_the_same_way():
    """F122 (500 dark pass): Settings › Backups & schedules said "monthly, 3:00 AM" while every other schedule
    reads "daily 2:00 AM" or "Sundays 6:00 PM", with no comma."""
    from scribe_bot.web.routes import SCHEDULE_INFO
    odd = [when for _, when in SCHEDULE_INFO.values()
           if not re.fullmatch(r"(daily|weekly|monthly|(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)days) \d{1,2}:\d{2} (AM|PM)", when)]
    assert not odd, odd


def test_the_load_tile_compares_like_with_like():
    """F123 (500 dark pass): Status › Load showed "0.7 of 8" and "Good" while its tip said "Good under 0.7 · Watch
    0.7–1": the tile shows the total across 8 threads but the ranges were per thread. The tip now uses the same total."""
    from scribe_bot.core import machine
    load = next(t for t in machine.tiles(machine.card({"load1": 0.7, "cpus": 8})) if t["key"] == "load")
    assert load["value"] == "0.7 of 8" and load["level"] == "ok"
    assert "Good under 5.6 · Watch 5.6–8 · Problem 8 or more" in load["tip"]
    assert machine.ranges_text("load") == "Good under 0.7 · Watch 0.7–1 · Problem 1 or more"   # legend: per thread


def test_one_pause_per_page(demo):
    """F124 (500 dark pass): Processing showed ❚❚ Pause in its top bar, on the running recording's card and again in
    that recording's history row; Status did the same. All of them pause everything. Pages with their own bar now
    show it once; Today, which has no bar, keeps the card's Pause."""
    for path in ("/processing", "/status"):
        page = demo.get(path).text
        assert 'class="progress-bar"' in page, path                    # a recording is running in the demo
        assert page.count('action="/processing/pause"') + page.count('action="/settings/pause"') == 1, path
    assert demo.get("/").text.count('action="/processing/pause"') >= 1


def test_pausing_from_status_cancels_a_timed_resume(demo):
    """F125: Processing › ☾ Resume at 1 AM, then Status › Pause, left the 1 AM restart in place."""
    from scribe_bot.pipeline import control
    demo.post("/processing/resume", data={"when": "tonight"})
    demo.post("/settings/pause", data={"on": "1"})
    page = demo.get("/processing").text
    assert "<b>Paused</b>" in page and "until" not in page.split('class="ctl-bar"')[1].split("</div>")[0]
    demo.post("/settings/pause", data={"on": "0"})
    assert control is not None and "Processing now" in demo.get("/processing").text


def test_headings_never_skip_a_level(demo):  # noqa: F811
    """F126 (2026-11-26): Inbox, People, Tasks, Processing and Your speaking went from the page title (h1) straight to
    h3 group headings, and a confirm page opened on its own had no h1, so screen readers' heading lists had gaps. Every
    page the app links to starts at h1 and never jumps more than one level down."""
    from urllib.parse import urlsplit

    from tests.unit.test_click_everything import SKIP, START, Page, kind
    seen, todo, bad = set(), list(START), []
    while todo:
        u = todo.pop()
        if not u.startswith("/") or SKIP.search(u) or kind(u) in seen:
            continue
        seen.add(kind(u))
        r = demo.get(u)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        p = Page()
        p.feed(r.text)
        todo += sorted(p.links)
        levels = [int(x) for x in re.findall(r"<h([1-6])\b", r.text.split("<main", 1)[-1])]
        if levels and levels[0] != 1:
            bad.append((urlsplit(u).path, "starts at h%d" % levels[0]))
        bad += [(urlsplit(u).path, "h%d then h%d" % (a, b)) for a, b in zip(levels, levels[1:]) if b > a + 1]
    assert len(seen) > 25 and not bad, bad


def test_every_open_fold_turns_its_chevron(demo):  # noqa: F811
    """F127 (2026-11-26, 500 dark pass): Help's open "What each button does" still pointed › like a closed fold; only
    Help had no [open] rule. Every fold with a chevron, on every page, turns it when open."""
    from tests.unit.test_click_everything import SKIP, START, Page, kind
    css = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text()
    seen, todo, classes = set(), list(START), set()
    while todo:
        u = todo.pop()
        if not u.startswith("/") or SKIP.search(u) or kind(u) in seen:
            continue
        seen.add(kind(u))
        r = demo.get(u)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        p = Page()
        p.feed(r.text)
        todo += sorted(p.links)
        for d in re.finditer(r'<details\b([^>]*)>\s*<summary\b[^>]*>(.*?)</summary>', r.text, re.S):
            if 'class="chev"' in d.group(2):
                c = re.search(r'class="([^"]*)"', d.group(1))
                classes.add(c.group(1) if c else "")
    assert len(classes) >= 5, classes
    for c in classes:
        assert any(re.search(r"\.%s\[open\][^{]*\.chev[^{]*\{[^}]*rotate" % re.escape(t), css) for t in c.split()), c


def test_every_more_menu_lists_the_same_places(demo):  # noqa: F811
    """F128 (2026-11-26, 500 dark pass): the More page listed Status, Costs, Processing history, Help and Settings, while
    ☰ › More also had Trash and Your speaking, in another order, and drew Help with another icon. Every More list now
    comes from one list."""
    def places(html, start, end):
        part = html[html.index(start):]
        part = part[:part.index(end)]
        return re.findall(r'<a (?:class="[^"]*" )?href="([^"]+)"[^>]*>\s*(?:<span class="nv-ic">)?<svg[^>]*><use href="[^"#]*#([\w-]+)"', part)
    more = places(demo.get("/more").text, 'class="card more-page"', "</div>")
    drawer = places(demo.get("/costs").text, 'aria-label="All places"', "</nav>")
    assert [h for h, _ in more] == ["/conversations?trash=1", "/speaking", "/costs", "/status", "/processing", "/help", "/settings"]
    assert more == drawer[-len(more):], (more, drawer)


def test_slash_focuses_a_visible_search_box_without_scrolling():
    """F129 (D-SP-7): "/" on a talk focused the sticky top bar's search box and the browser scrolled the talk back to
    its top; under 960 px it picked the hidden top-bar box, so nothing happened. It now takes the first box you can see
    and keeps your place."""
    js = (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.js").read_text()
    branch = js[js.index('} else if (e.key === "/") {'):]
    branch = branch[:branch.index("} else if", 10)]
    assert "offsetParent !== null" in branch and "focus({ preventScroll: true })" in branch
    assert "document.querySelector('input[name=\"q\"]')" not in branch


def test_a_search_hit_by_an_unnamed_voice_says_who(demo):  # noqa: F811
    """F130 (500 dark pass): Search "budget" › a line said by a voice not named yet showed "Line · line 14" with no
    speaker, while every other line named one. It now says "Speaker N", the same as the talk page."""
    from scribe_bot.ai import extract
    conn = demo.db()
    row = conn.execute("SELECT g.conversation_id AS cid, g.speaker_id AS sid, g.text FROM segment g JOIN speaker sp ON sp.id = g.speaker_id "
                       "JOIN conversation c ON c.id = g.conversation_id "
                       "WHERE sp.person_id IS NULL AND c.deleted_at IS NULL AND g.text LIKE '%budget%' LIMIT 1").fetchone()
    assert row, "the demo has a line about the budget by an unnamed voice"
    n = extract.speaker_numbers(conn, row["cid"])[row["sid"]]
    html = demo.get("/search", params={"q": "budget", "conv": row["cid"]}).text
    assert "<span>Speaker %d</span>" % n in html


def test_the_demo_bell_agrees_with_costs(demo):  # noqa: F811
    """F131 (500 dark pass): the demo's bell said "AI spending reached 50% of this month's cap" while Costs showed 8%
    of the cap. No made-up alert may claim a share of the cap that Costs doesn't show."""
    conn = demo.db()
    pct = int(re.search(r"(\d+)% of the \$[\d.]+ monthly cap", demo.get("/costs").text).group(1))
    for (title,) in conn.execute("SELECT title FROM notification"):
        m = re.search(r"(\d+)% of (?:this month's|the [^ ]+ monthly) cap", title)
        assert not m or int(m.group(1)) <= pct, (title, pct)


def test_what_to_check_counts_only_open_tasks(demo):  # noqa: F811
    """F132 (500 dark pass): a talk whose only task of yours was done still said "What to check · 1 you owe"."""
    conn = demo.db()
    cid = conn.execute("SELECT id FROM conversation c WHERE deleted_at IS NULL AND EXISTS (SELECT 1 FROM task t WHERE t.conversation_id = c.id "
                       "AND t.owner_person_id = 1 AND t.status = 'done') LIMIT 1").fetchone()[0]
    n_open = conn.execute("SELECT count(*) FROM task WHERE conversation_id = ? AND owner_person_id = 1 AND status = 'open'", (cid,)).fetchone()[0]
    strip = demo.get(f"/conversations/{cid}").text.split('id="check-strip"', 1)[1].split("</nav>", 1)[0]
    shown = re.search(r'>(\d+) you owe<', strip)
    assert (int(shown.group(1)) if shown else 0) == n_open, (cid, n_open, strip)


def test_compare_on_a_demo_talk_offers_the_other_model(demo):  # noqa: F811
    """F133: a demo talk's notes are labelled 'demo', but the ledger says Claude Sonnet made them. The ⋯ menu offered
    'Compare with Claude Sonnet', the same model. It must offer the one that didn't make the notes."""
    from scribe_bot.ai import extract
    conn = demo.db()
    row = conn.execute("SELECT c.id, l.model FROM conversation c JOIN ledger_entry l ON l.conversation_id = c.id "
                       "WHERE c.processed_by = 'demo' AND c.deleted_at IS NULL AND l.status = 'ok' "
                       "AND l.model LIKE 'claude%' LIMIT 1").fetchone()
    assert row, "demo data has a talk with an AI ledger row"
    cid, made_by = row
    other = extract.other_model(conn, cid)[1]
    assert ("sonnet" in made_by) != ("sonnet" in other), (made_by, other)
    html = demo.get(f"/conversations/{cid}").text
    assert "Compare with Claude %s" % ("Sonnet" if "sonnet" in made_by else "Haiku") not in html
    assert "Compare with Claude" in html


def _crawl(demo):  # noqa: F811
    """Every kind of page the app links to, once each: yields (path, html)."""
    from urllib.parse import urlsplit

    from tests.unit.test_click_everything import SKIP, START, Page, kind
    seen, todo = set(), list(START)
    while todo:
        u = todo.pop()
        if not u.startswith("/") or SKIP.search(u) or kind(u) in seen:
            continue
        seen.add(kind(u))
        r = demo.get(u)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        p = Page()
        p.feed(r.text)
        todo += sorted(p.links)
        yield u, r.text


def test_every_list_you_can_pick_from_has_a_select_button(demo):  # noqa: F811
    """D-SP-1 (Brian, 2026-11-26: 1A): Inbox and People had a Select button but Tasks didn't, so on a phone picking was a
    hidden long-press. Sweep: Trash had the same gap. Every list you can pick from, with 2+ rows, has a Select button."""
    pages, bad = 0, []
    for u, html in _crawl(demo):
        for lid in re.findall(r'id="([\w-]+)"[^>]*\bdata-picker\b', html):
            pages += 1
            if html.count("data-pick-id=") > 1 and f'data-pk-start="{lid}"' not in html:
                bad.append((u, lid))
    assert pages >= 3 and not bad, bad


def test_a_link_that_only_opens_a_page_is_never_red():
    """D-SP-2 (Brian, 2026-11-26: 2A): the Talks toolbar's Trash link was red, so it read as Delete though it only opens
    Trash. Red is for buttons that delete; no link (an <a>, which only opens a page) is styled red."""
    css = re.sub(r"/\*.*?\*/", "", (Path(__file__).resolve().parents[2] / "scribe_bot" / "web" / "static" / "app.css").read_text(), flags=re.S)
    bad = [sel.strip() for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
           if re.search(r"(^|[\s,>])a([.\[:]|\s*$|\s*,)", sel.strip())
           and re.search(r"(?<![-\w])color:[^;]*(--bad|#bd4256|#f28ba0|#c0392b|\bred\b)", body)]
    assert not bad, bad


def test_work_and_home_are_the_same_pill_on_every_page(demo):  # noqa: F811
    """D-SP-3 (Brian, 2026-11-26: 3B): Talks showed the area as a dot and a word, People, Search and Today as a pill.
    Every page draws it as the same pill."""
    bad, pills = [], 0
    for u, html in _crawl(demo):
        pills += len(re.findall(r'class="badge badge-(?:work|personal|school|other)"', html))
        if re.search(r'class="cr-area\b', html):
            bad.append(u)
    assert pills and not bad, bad
    assert 'badge badge-work' in demo.get("/conversations").text


def test_every_back_link_is_the_same_text_link(demo):  # noqa: F811
    """D-SP-4 (Brian, 2026-11-26: 4A): most pages said "‹ Today" in grey text, but a talk had an arrow icon and a bold
    "Talks". Every back link on every page is the same text link: "‹ <where it goes>"."""
    bad, n = [], 0
    for u, html in _crawl(demo):
        for cls, inner in re.findall(r'<a class="([^"]*\b(?:back-link|cv-back)\b[^"]*)"[^>]*>(.*?)</a>', html, re.S):
            n += 1
            if "back-link" not in cls.split() or not inner.startswith("‹ ") or "<" in inner:
                bad.append((u, cls, inner[:60]))
    assert n > 15 and not bad, bad


def test_talk_time_says_what_it_covers_and_who_the_rest_is(demo):  # noqa: F811
    """D-SP-5 (Brian, 2026-11-26: 5A): "Talk time 7 min · Priya 40% · you 35%" didn't say the minutes are every talk you
    were both in, or who the other 25% is. Now "3 talks, 7 min" and "Priya 40%, you 35%, others 25%", adding to 100."""
    seen = 0
    for (pid,) in demo.db().execute("SELECT id FROM person WHERE id != 1"):
        html = demo.get(f"/people/{pid}").text
        m = re.search(r'id="talk-time">.*?<small>(.*?)</small></span><span class="r1-v">(.*?)</span>', html, re.S)
        if not m:
            continue
        seen += 1
        assert re.fullmatch(r"\d+ talks?, .+", m.group(2)), m.group(2)
        shares = [int(x) for x in re.findall(r"(\d+)%", m.group(1))]
        assert ", you " in m.group(1) and sum(shares) == 100 or (len(shares) == 2 and sum(shares) == 100), m.group(1)
    assert seen


def test_right_click_on_a_talk_offers_both_exports_named_like_its_menu(demo):  # noqa: F811
    """D-SP-6 (Brian, 2026-11-26: 6A): right-clicking a talk in Talks offered only "Export (.md)" while the talk's own ⋯
    offered .md and PDF. The right-click now offers both, with the same names, and both downloads work."""
    root = Path(__file__).resolve().parents[2] / "scribe_bot" / "web"
    js = (root / "static" / "ui.js").read_text()
    tpl = (root / "templates" / "conversation.html").read_text()
    for label in ("Export as text file (.md)", "Export as PDF"):
        assert f'"{label}"' in js and label in tpl, label
    assert 'dl("md")' in js and 'dl("pdf")' in js
    cid = demo.db().execute("SELECT id FROM conversation WHERE deleted_at IS NULL AND summary IS NOT NULL LIMIT 1").fetchone()[0]
    for ext, kind in (("md", "text/"), ("pdf", "application/pdf")):
        r = demo.get(f"/conversations/{cid}/export.{ext}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), (ext, r.status_code)


def _theme_tokens() -> dict[str, dict[str, str]]:
    """The colour tokens of the light theme and the picked-Dark theme, as {name: '#rrggbb'}."""
    css = _static("app.css")
    light = css[css.index(":root {"):css.index("/* Dark theme")]
    dark = css[css.index(':root[data-theme="dark"] {'):]
    dark = dark[:dark.index("}")]
    return {name: dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", block)) for name, block in (("light", light), ("dark", dark))}


def test_f134_a_picked_segment_stands_out_from_its_track_in_both_themes():
    """F134 (live, Dark at 1440): the side bar's area switch painted its picked choice with --surface, which is
    darker than the grey track in dark, so "All" sank out of sight. Every picked segment, pane-layout button and
    focused search box sits on --surface-2 and must be clearly lighter than it, in light and in dark."""
    css = _static("app.css")
    picked = re.findall(r"([^{}]*(?:\.on\b|:checked|:focus-within)[^{}]*)\{([^}]*)\}", css)
    paints = {sel.strip(): m.group(1) for sel, body in picked
              if (m := re.search(r"background:\s*var\(--([\w-]+)\)", body)) and re.search(r"\.seg|\.dp-lay|\.dsearch", sel)}
    assert len(paints) >= 4, paints
    for theme, tok in _theme_tokens().items():
        track = _lum(tok["surface-2"])
        for sel, name in paints.items():
            assert _lum(tok[name]) - track > (0.02 if theme == "light" else 0.04), (theme, sel, name)


def _swipe_block() -> str:
    js = _static("app.js")
    return js[js.index("/* Swipe rows, v3"):js.index("/* Tap outside to get out")]


def test_f141_swipe_buttons_fill_the_gap_and_settle_on_a_spring():
    """F141 (Brian, 2026-11-26: "the swiping animation is very poor … the animation and color and ux of it sucks").
    The buttons take an equal share of the gap the row leaves (no label cut in half), fade and grow in, and the first
    one takes the strip with a pop past the point of no return. The row settles on a spring at the finger's speed, a
    leaving row flies off and can't be grabbed mid-flight, and reduced motion skips the animation."""
    js, css = _swipe_block(), _static("app.css")
    assert 'L.style.setProperty("--p"' in js and 'L.style.setProperty("--n", a.length)' in js
    assert ".sw-b { flex: 0 0 calc(var(--p, 0) * 1px / var(--n, 1))" in css
    assert "opacity: clamp(0, calc((var(--p, 0) / var(--n, 1) - 18) / 34), 1)" in css
    assert ".sw-layer.sw-full .sw-b:first-child .sw-in, .sw-layer.sw-pick .sw-go .sw-in { opacity: 1; transform: scale(1.12) }" in css
    assert "requestAnimationFrame(step)" in js and "prefers-reduced-motion: reduce" in js
    assert "if (h._leaving) { st = null; return; }" in js
    assert "sw-anim" not in css + js and "transitionDuration" not in js   # the old CSS-timed settle is gone


def test_f141_every_swipe_colour_has_a_light_and_two_dark_shades(demo):
    """Every colour a row's swipe action names has its own shade in light, in system dark and in picked Dark, and the
    strip behind the buttons is that colour (no page showing through between buttons)."""
    import html as _h
    import json
    css = _static("app.css")
    used = set()
    for _, page in _crawl(demo):
        for raw in re.findall(r"data-sw='([^']*)'", page):
            for side in json.loads(_h.unescape(raw)).values():
                used |= {a.get("c", "ink") for a in side}
    assert used >= {"red", "green", "ink"}, used
    for c in used:
        assert re.search(rf"\.sw-c-{c} \{{ --sw-c: #[0-9a-f]{{6}} \}}", css), c
        assert f':root:not([data-theme="light"]) .sw-c-{c} {{ --sw-c:' in css, c
        assert f':root[data-theme="dark"] .sw-c-{c} {{ --sw-c:' in css, c
    assert ".sw-layer { position: absolute; z-index: 0; display: flex; overflow: hidden; background: var(--sw-c) }" in css


def test_f139_holding_a_transcript_line_copies_it_on_a_phone(demo):
    """F139 (Brian, 2026-11-26: "hold a line to copy it" didn't work on his phone; only a computer's right-click had
    Copy line). A touch hold on a line copies time, who and words, lights the line, and its lift doesn't open Fix words."""
    ui = _static("ui.js")
    block = ui[ui.index("/* F139"):ui.index("function lineMenu")]
    assert 'e.pointerType !== "touch"' in block and 'closest(e.target, ".lines .line[data-seg]")' in block
    assert 'copy(txt, "Line")' in block and 'classList.add("holding")' in block
    assert 'document.addEventListener("click", function (e) { if (justHeld(e.target))' in block
    assert "Copy line\", fn: function () { copy(lineText(line), \"Line\")" in ui   # right-click copies the same text
    assert ".lines .line.holding {" in _static("app.css")
    page = demo.get("/conversations/1?tx=1").text
    line = page[page.index('class="line'):]
    line = line[:line.index("</details>")]
    assert 'data-seg="' in line and 'class="who"' in line and "data-edit-url" in line and 'class="t"' in line


def _z(css: str, selector: str) -> list[int]:
    return [int(z) for z in re.findall(re.escape(selector) + r"\s*\{[^}]*?z-index:\s*(\d+)", css)]


def test_f140_the_peeking_side_bar_floats_over_the_top_bar():
    """F140 (Brian, 2026-11-26): hovering the folded side bar opened it under the search bar, because the top bar was
    lifted over it (R4-06). The side bar, and above all its peek, now sit over the top bar; only the top bar's own open
    menus (and sheets) go higher."""
    css = _static("app.css")
    top = max(_z(css, ".dtop"))
    assert min(_z(css, "html.side-rail .sidebar.peek")) > top
    side = [int(z) for z in re.findall(r"  \.sidebar \{[^}]*z-index:\s*(\d+)", css)]
    assert side and min(side) > top, (side, top)
    assert max(_z(css, ".dtop:has(details[open])")) > min(_z(css, "html.side-rail .sidebar.peek"))   # its menus still win


def _lstar(h: str) -> float:
    """CIE lightness (0-100) of a #rrggbb colour, which tracks how different two greys look."""
    c = [int(h.lstrip("#")[k:k + 2], 16) / 255 for k in (0, 2, 4)]
    c = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in c]
    y = .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
    return 116 * (y ** (1 / 3) if y > .008856 else 7.787 * y + 16 / 116) - 16


def test_f142_a_hover_fill_can_be_seen_on_the_page_and_on_cards_in_both_themes():
    """F142 (live, Light at 1440): the bell, the status ring, transcript lines and other hovers on the page
    background used --surface-2, which in light is 1.7 lightness points from the page, so hovering showed
    nothing. Every hover that fills a background uses --hover, which stands clearly off the page, the cards and
    a resting grey (--surface-2) in light and in dark."""
    rules = _css_rules(_static("app.css"))
    hovers = [(sel, body) for _, sel, body in rules if ":hover" in sel and re.search(r"background:\s*var\(--", body)]
    assert len(hovers) >= 20, len(hovers)
    faint = [sel for sel, body in hovers if re.search(r"background:\s*var\(--(surface-2|surface|bg)\)", body)]
    assert not faint, faint
    for theme, tok in _theme_tokens().items():
        h = _lstar(tok["hover"])
        for under in ("bg", "surface", "surface-2"):
            assert abs(h - _lstar(tok[under])) >= 4, (theme, under, round(h, 1), round(_lstar(tok[under]), 1))


def test_f143_processing_history_card_keeps_its_gap_from_the_job_above():
    """F143 (live, 500 Dark): on Processing history the "This week" card sat 2 px under the running job's card,
    while every other card on the page is 12 px apart."""
    css = _static("app.css")
    assert re.search(r"#history-list\s*\{[^}]*margin-top:\s*12px", css)
    tpl = (WEB / "templates" / "processing.html").read_text()
    assert 'style="margin-top' not in tpl

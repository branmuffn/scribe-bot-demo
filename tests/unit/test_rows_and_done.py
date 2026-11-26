"""Rows, icons, done and select (Brian, 2026-11-26, on his Pixel and a computer): "i still don't think the logo of the
inbox is centered within the box", "i don't love how each page has it's own shape", "the circle for task is the same
for selecting as for marking done" and "the marking as done ... is kind of slow" (F135–F138). Made-up demo data only."""
import re
from pathlib import Path

from tests.unit.test_public_demo import demo  # noqa: F401  (the fixture)
from tests.unit.test_screen_pass import _crawl, _css_rules

WEB = Path(__file__).resolve().parents[2] / "scribe_bot" / "web"


def _static(name: str) -> str:
    return (WEB / "static" / name).read_text()


def _tpl(name: str) -> str:
    return (WEB / "templates" / name).read_text()


def _classes(tag: str) -> set[str]:
    m = re.search(r'class="([^"]*)"', tag)
    return set(m.group(1).split()) if m else set()


def _path_bbox_x(d: str) -> tuple[float, float]:
    """The x extent of an SVG path's end points, for one made of M, l and a (rounded corner) steps."""
    x, xs = 0.0, []
    for cmd, args in re.findall(r"([MmLlAaZz])([^MmLlAaZz]*)", d):
        n = [float(v) for v in re.findall(r"-?\d*\.?\d+", args)]
        if cmd == "M":
            x = n[0]
        elif cmd == "l":
            x += n[0]
        elif cmd == "a":
            x += n[5]
        xs.append(x)
    return min(xs), max(xs)


def test_f135_the_inbox_play_triangle_sits_in_the_optical_middle():
    """F135: the ▶ had its centre of weight in the middle, but its rounded tip made that overshoot (its box sat 2.2 of
    24 units right of centre, 1.3 px in the Inbox's 32 px box). Optical centre: about half way, 0.5–1.5 units right."""
    path = re.search(r'<symbol id="i-play-solid" viewBox="0 0 24 24"><path d="([^"]+)"', _tpl("icons.html")).group(1)
    lo, hi = _path_bbox_x(path)
    hi += 1 - (1 - .85 ** 2) ** .5          # the rounded tip (r = 1, chord 1.7) bulges past its two end points
    assert 12.5 <= (lo + hi) / 2 <= 13.5, (lo, hi)


def test_f135_round_row_icons_are_drawn_not_typed(demo):  # noqa: F811
    """F135 sweep: a typed ? ＋ ☾ ✓ ≡ sits on the font's line, never in the middle of its circle (People's "names
    mentioned" / "voices not named", Review's speakers, Today's overnight card, the Inbox sheet's Type and Tonight)."""
    bad = []
    for u, html in _crawl(demo):
        for m in re.finditer(r'<(span|i)\s+class="[^"]*\b(lead|ib|oc-ic)\b[^"]*"[^>]*>(.*?)</\1>', html, re.S):
            inner = re.sub(r"<svg.*?</svg>", "", m.group(3), flags=re.S).strip()
            if inner and not re.fullmatch(r"[A-Z]{1,2}", inner):   # a person's initials are letters
                bad.append((u, m.group(0)[:90]))
    assert not bad, bad


LEADS = ("av", "cr-ic", "oc-ic")


def test_f136_every_list_rows_leading_icon_is_the_one_round_shape(demo):  # noqa: F811
    """F136: Inbox's ▶ was a rounded square, Talks and People circles. Every row's leading icon, on every page, is a
    .lead: one CSS rule makes it a circle of one size (--lead), centred the same way, in light and dark."""
    bad, seen = [], set()
    for u, html in _crawl(demo):
        for m in re.finditer(r"<span\s[^>]*>", html):
            c = _classes(m.group(0))
            if c & set(LEADS) and "av-lg" not in c:
                seen.add(next(iter(c & set(LEADS))))
                if "lead" not in c:
                    bad.append((u, m.group(0)[:80]))
        # an icon box at the start of an Inbox, People or Review row
        for m in re.finditer(r'<summary class="[^"]*\b(in-sum|sp-sum|mention-row)\b[^"]*">(.*?)</summary>', html, re.S):
            ib = re.search(r'<span class="[^"]*\bib\b[^"]*"', m.group(2))
            if ib and "lead" not in _classes(ib.group(0)):
                bad.append((u, ib.group(0)))
            if ib:
                seen.add(m.group(1))
    assert not bad, bad
    assert {"av", "cr-ic", "in-sum"} <= seen, seen
    rules = _css_rules(_static("app.css"))
    lead = [b for ctx, sel, b in rules if not ctx and sel == ":is(.item, .in-sum, .sp-sum, .overnight-card) > .lead"]
    assert lead and "border-radius: 50%" in lead[0] and "width: var(--lead)" in lead[0] and "height: var(--lead)" in lead[0]
    assert "place-items: center" in lead[0]
    css = _static("app.css")
    assert 'html[data-density="compact"] { --lead: 32px }' in css and 'html[data-density="comfortable"] { --lead: 44px }' in css
    assert "Brian, 2026-11-26" in css.split("/* F136")[1][:200]   # the choice is explained where it's made


def test_f137_done_and_select_are_different_controls(demo):  # noqa: F811
    """F137: the task's done circle and the select tick were both circles. Done = a circle (fills green with a ✓);
    select = the square box every list uses, on phones (in the circle's place) and computers (its own gutter)."""
    html = demo.get("/tasks").text
    rows = re.findall(r'<div class="item task-row[^"]*" id="task-\d+".*?</form>', html, re.S)
    assert rows
    for r in rows:
        cb = re.search(r'<span class="([^"]*)" data-pk-cb role="checkbox"', r)
        tick = re.search(r'<form [^>]*class="tick-f"[^>]*><button class="(tick[^"]*)"', r)
        assert cb and tick and "pk-cb" in cb.group(1).split() and "tick" not in cb.group(1).split()
        assert "pk-cb" not in tick.group(1) and tick.group(1).split()[0] == "tick"
    rules = _css_rules(_static("app.css"))
    round_pick = [(ctx, sel) for ctx, sel, b in rules if "pk-cb" in sel and "border-radius: 50%" in b]
    assert not round_pick, round_pick                                  # select is never a circle
    tick = [b for ctx, sel, b in rules if sel == ".tick"]
    assert tick and "border-radius: 50%" in tick[-1]
    assert any(sel == ".tick::after" and "content: none" in b for _, sel, b in rules)   # the typed ✓ is gone
    phone_box = [b for ctx, sel, b in rules if "picking" in sel and ".lead::before" in sel and "border-radius: 7px" in b]
    assert phone_box   # a row's round icon becomes the square box while picking on a phone
    desk = [b for ctx, sel, b in rules if sel == "#task-list[data-picker] .task-row > .pk-cb"]
    assert desk and "width" not in desk[-1] and "border-radius" not in desk[-1]   # the same 18 px box as Talks and People


def test_f138_done_answers_with_a_small_note_and_undo_brings_it_back(demo):  # noqa: F811
    """F138: Done waited for a redirect and a whole Tasks page. Sent by the page (X-Quick: 1), it gets a small note
    with the Undo bar's id instead; Undo the same way puts the task back. A plain post (no JS) still redirects."""
    conn = demo.db()
    tid = conn.execute("SELECT id FROM task WHERE status = 'open' ORDER BY id LIMIT 1").fetchone()[0]
    r = demo.post(f"/tasks/{tid}/done", headers={"X-Quick": "1", "referer": "http://testserver/tasks"}, follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    j = r.json()
    assert j["ok"] and j["undo"]["label"] == "Marked done" and len(r.content) < 400
    assert demo.db().execute("SELECT status FROM task WHERE id = ?", (tid,)).fetchone()[0] == "done"
    u = demo.post(f"/undo/{j['undo']['id']}", headers={"X-Quick": "1", "referer": "http://testserver/tasks"}, follow_redirects=False)
    assert u.status_code == 200 and u.json()["ok"]
    assert demo.db().execute("SELECT status FROM task WHERE id = ?", (tid,)).fetchone()[0] == "open"
    again = demo.post(f"/undo/{j['undo']['id']}", headers={"X-Quick": "1"}, follow_redirects=False)
    assert again.status_code == 410 and "undo" in again.json()["detail"].lower()   # the page shows this note
    plain = demo.post(f"/tasks/{tid}/done", headers={"referer": "http://testserver/tasks"}, follow_redirects=False)
    assert plain.status_code == 303


def test_f138_the_page_sends_quick_actions_itself_and_keeps_undo():
    """F138: the done form is no longer sent by htmx (post, redirect, whole page, then the swap): app.js shows the
    change first, sends it with X-Quick in the background, puts the row back on Undo or on a failure."""
    js = _static("app.js")
    block = js[js.index("/* F138"):]
    for part in ('f.getAttribute("data-quick")', '"X-Quick": "1"', "e.stopImmediatePropagation()",
                 "data-quick-undo", "putBack(s)", "note(fail(err))", "uncap(s.parent)"):
        assert part in block, part
    assert block.index('addEventListener("submit"') < block.index('f.getAttribute("data-quick")')
    css = _static("app.css")
    assert ".qa-fold {" in css and "@keyframes tick-pop" in css


def test_f138_every_quick_action_is_marked(demo):  # noqa: F811
    """F138 sweep: the same instant path for Tasks (Done, Reopen, Not needed, Delete, Keep, Remind), the bell's Dismiss
    and Restore, Inbox Delete, Review's Keep / Remove / Dismiss / Decline / Add, Trash Restore. A talk's own task list
    (done tasks stay there) marks the circle at once and lets htmx swap in the saved rows."""
    want = {"/tasks": r"/tasks/\d+/(done|not-needed|delete)$", "/tasks?show=done": r"/tasks/\d+/reopen$",
            "/notifications": r"/notifications/\d+/dismiss$", "/inbox": r"/review/\d+/dismiss$",
            "/": r"/tasks/\d+/done$", "/review": r"/(review/\d+/(dismiss|approve)|tasks/\d+/(file|delete))$",
            "/tasks?tab=inbox": r"/tasks/\d+/(file|delete)$", "/conversations?trash=1": r"/(conversations|recordings)/\d+/(restore|unskip)$"}
    for page, pat in want.items():
        forms = [f for f in re.findall(r"<form\s[^>]*>", demo.get(page).text) if re.search(pat, (re.search(r'action="([^"]*)"', f) or [None, ""])[1])]
        assert forms, page
        bad = [f for f in forms if 'data-quick="' not in f]
        assert not bad, (page, bad[:2])
    cid = demo.db().execute("SELECT conversation_id FROM task WHERE conversation_id IS NOT NULL LIMIT 1").fetchone()[0]
    talk = demo.get(f"/conversations/{cid}").text
    ticks = re.findall(r'<form [^>]*class="tick-f"[^>]*>', talk)
    assert ticks and all('data-quick="mark"' in t for t in ticks)
    # each answers with the small note, not a page
    nid = demo.db().execute("SELECT id FROM notification WHERE dismissed_at IS NULL LIMIT 1").fetchone()[0]
    r = demo.post(f"/notifications/{nid}/dismiss", headers={"X-Quick": "1", "referer": "http://testserver/notifications"}, follow_redirects=False)
    assert r.status_code == 200 and r.json()["undo"]["label"] == "Notification dismissed"


def _lstar(h: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    y = .2126 * lin(r) + .7152 * lin(g) + .0722 * lin(b)
    return 116 * (y ** (1 / 3) if y > 216 / 24389 else (24389 / 27 * y + 16) / 116) - 16


def _mix(a: str, b: str, p: float) -> str:   # color-mix(in srgb, a p, b)
    return "#" + "".join("%02x" % round(int(a[i:i + 2], 16) * p + int(b[i:i + 2], 16) * (1 - p)) for i in (1, 3, 5))


def test_f136_every_clickable_list_row_gets_the_same_hover_tint(demo):  # noqa: F811
    """F136 sweep (found live): a Talks row tinted on hover, a Tasks row didn't, Inbox used another tint. Every row you
    can click (a tap-row, a task or note that opens its sheet, a row that is a link or a summary) on every page is
    covered by one hover rule, the last word on row hover, and the tint shows: 4+ L* from the card in light and dark."""
    css = _static("app.css")
    rules = _css_rules(css)
    mine = [(i, sel) for i, (ctx, sel, b) in enumerate(rules) if "(hover: hover)" in ctx and "var(--row-hover)" in b]
    assert len(mine) == 1, mine
    at, sel = mine[0]
    covered = set(re.findall(r"\.([\w-]+)", sel))
    rowsel = re.compile(r"\.(%s)\b" % "|".join(sorted(covered)))
    later = [s for ctx, s, b in rules[at + 1:] if ":hover" in s and rowsel.search(s) and "background" in b]
    assert not later, later
    kinds, bad = set(), []
    for u, html in _crawl(demo):
        for m in re.finditer(r'<(div|a|summary|details)\s+class="([^"]*)"[^>]*>(?=(.{0,1500}))', html, re.S):
            tag, cls, after = m.group(1), set(m.group(2).split()), m.group(3)
            clickable = ("item" in cls and ("tap-row" in cls or re.match(r'(?:(?!<div class="item[ "]).)*data-open-sheet', after, re.S))) \
                or (tag == "a" and "row1" in cls) or (tag == "summary" and cls & {"in-sum", "sp-sum", "mention-row"})
            if not clickable:
                continue
            row = cls | ({"in-row"} if "in-sum" in cls else set()) | ({"sp-row"} if "sp-sum" in cls else set())
            kinds.add(next(iter(sorted(row & covered)), "?"))
            if not row & covered:
                bad.append((u, sorted(cls)))
    assert not bad, bad[:5]
    assert {"tap-row", "task-row", "in-row", "row1"} <= kinds, kinds
    p = float(re.search(r"--row-hover: color-mix\(in srgb, var\(--acc\) (\d+)%, var\(--surface\)\)", css).group(1)) / 100
    tok = {k: re.findall(rf"--{k}: (#[0-9a-f]{{6}})", css) for k in ("acc", "surface")}
    for acc, surf in ((tok["acc"][0], tok["surface"][0]), (tok["acc"][1], tok["surface"][1])):   # light, dark
        assert abs(_lstar(_mix(acc, surf, p)) - _lstar(surf)) >= 4, (acc, surf, p)


def test_f137_help_and_tour_describe_the_new_done_and_select(demo):  # noqa: F811
    """F137 (Brian, 2026-11-26): help said "The circle: Done." and "initials circles become round checkboxes", and the
    tour named a ⋯ button that is gone. Help and the tour describe the green done circle and the square select box."""
    html = demo.get("/help").text
    dl = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    assert "round checkbox" not in dl
    assert re.search(r"The done circle .{0,80}green", dl), dl[:200]
    for who in ("Tasks › Select", "People › Select"):
        assert re.search(re.escape(who) + r".{0,160}square", dl), who
    tour = re.search(r"var TOUR = \[(.*?)\];", _static("app.js"), re.S).group(1)
    assert "The ⋯ button" not in tour and re.search(r"circle.{0,40}green", tour)

"""Heat guard, Auto speed and Night only (Brian, 2026-11-21: "heat OK", Auto must notice SSH, Screen Sharing, RustDesk and
Claude / Codex chats too, Night only after 1 AM)."""
import os
import time
from datetime import datetime
from types import SimpleNamespace

from scribe_bot.core import db, queue
from scribe_bot.pipeline import pace


def conn_(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    pace._state.update(threads=4, last_use=0.0, why="", hot_seen=None, cool_since=None)
    return c


def fake_run(outputs):
    def run(args, **_):
        return SimpleNamespace(stdout=outputs.get(args[0], ""))
    return run


def test_parsers():
    assert pace.parse_hid_idle('    | |   "HIDIdleTime" = 61000000000') == 61
    who = ("user console  Nov 11 15:24 01:08 172\n"
           "user ttys000  Nov 14 23:30   .   13746\n"                       # local terminal: no (host)
           "user ttys003  Nov 21 14:00   .   2222 (192.0.2.20)\n")           # SSH, typing now
    assert pace.parse_who(who) == 1
    assert pace.parse_who("user ttys004  Nov 21 09:00 02:10 3333 (192.0.2.20)") == 0   # idle 2 h
    assert pace.parse_who("user ttys004  Nov 21 09:00 00:12 3333 (192.0.2.20)") == 1
    assert pace.parse_remote("/Applications/RustDesk.app/Contents/MacOS/RustDesk --cm", "") == ["RustDesk"]
    assert pace.parse_remote("/Applications/RustDesk.app/Contents/MacOS/RustDesk --server", "") == []
    assert pace.parse_remote("", "tcp4  0  0  192.0.2.10.5900  192.0.2.20.50123  ESTABLISHED") == ["Screen Sharing"]


def test_in_use_sees_chats_and_input(tmp_path):
    idle = {"ioreg": '"HIDIdleTime" = 999000000000'}
    assert pace.in_use(fake_run(idle), home=tmp_path, night=False) == ""
    assert pace.in_use(fake_run({"ioreg": '"HIDIdleTime" = 5000000000'}), home=tmp_path) == "keyboard or mouse"
    chat = tmp_path / ".claude/projects/x"
    chat.mkdir(parents=True)
    (chat / "s.jsonl").write_text("{}")
    busy = dict(idle, ps="  0 200.0 node")   # 2026-11-22: a chat counts only while the Mac is busy
    assert pace.in_use(fake_run(busy), home=tmp_path, night=False) == "a busy Claude chat"
    old = time.time() - 3600
    os.utime(chat / "s.jsonl", (old, old))
    assert pace.in_use(fake_run(busy), home=tmp_path, night=False) == ""


def test_auto_goes_gentle_while_used_and_back_after_10_min(tmp_path):
    c = conn_(tmp_path)
    t = 1_000_000.0
    assert pace.threads_for(c, "", t) == 4
    assert pace.threads_for(c, "SSH", t) == 2
    assert pace.threads_for(c, "", t + 9 * 60) == 2
    assert pace.threads_for(c, "", t + 11 * 60) == 4
    pace.set_mode(c, "full")
    assert pace.threads_for(c, "SSH", t) == 4
    pace.set_mode(c, "gentle")
    assert pace.threads_for(c, "", t) == 2


def test_heat_guard_pauses_after_two_hot_checks_and_resumes_when_cool(tmp_path):
    c = conn_(tmp_path)
    said = []
    t = 1_000_000.0
    assert pace.heat_step(c, 97, t, said.append) is None          # first hot check
    assert not queue.hold_requested(c)
    assert pace.heat_step(c, 96, t + 300, said.append) == "paused"
    assert queue.hold_requested(c) and "hot" in said[-1]          # running stages stop at their next safe point
    assert pace.heat_step(c, 79, t + 360, said.append) is None    # cool, but not for 5 min yet
    assert pace.heat_step(c, 78, t + 360 + 300, said.append) == "resumed"
    assert not queue.hold_requested(c)


def test_heat_guard_gives_up_after_2_hours_on_half_the_cores(tmp_path):
    c = conn_(tmp_path)
    t = 1_000_000.0
    pace.heat_step(c, 97, t)
    pace.heat_step(c, 97, t + 300)
    assert pace.heat_step(c, 90, t + 300 + 2 * 3600) == "resumed_gentle"
    assert not pace.heat_paused(c)
    assert pace.threads_for(c, "", t + 300 + 2 * 3600 + 60) == 2


def test_night_only_holds_new_recordings_until_1am(tmp_path):
    c = conn_(tmp_path)
    pace.set_mode(c, "night")
    assert pace.holds_new_work(c, datetime(2026, 11, 21, 23, 30).astimezone())
    assert not pace.holds_new_work(c, datetime(2026, 11, 22, 1, 5).astimezone())
    assert pace.holds_new_work(c, datetime(2026, 11, 22, 7, 0).astimezone())
    queue.enqueue(c, "recording", None, {})
    other = queue.enqueue(c, "ai_extract", None, {})
    assert queue._claim(c, ("recording",))["id"] == other          # AI jobs still run; the recording waits


def test_processing_page_speed_picker(tmp_path):
    from fastapi.testclient import TestClient

    from scribe_bot.core.settings import Settings
    from scribe_bot.web.app import create_app
    cfg = Settings(tmp_path)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    c.close()
    client = TestClient(create_app(cfg))
    page = client.get("/processing").text
    assert "Night only" in page and 'value="auto" class="seg-opt on"' in page
    client.post("/processing/speed", data={"mode": "night"})
    assert 'value="night" class="seg-opt on"' in client.get("/processing").text

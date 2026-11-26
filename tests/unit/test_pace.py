"""Heat guard, Auto speed and Night only (Brian, 2026-11-21: "heat OK", Auto must notice SSH, Screen Sharing, RustDesk and
Claude / Codex chats too, Night only after 1 AM)."""
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


def test_auto_judges_by_real_cpu_and_memory_use():
    """Brian, 2026-11-22: judge by how busy the Mac actually is, not keyboard / RustDesk / open chats."""
    calm = {"ps": "  0  12.0 /usr/bin/claude\n 10 390.0 python\n  0 300.0 com.docker.virtualization\n",
            "memory_pressure": "System-wide memory free percentage: 84%"}
    assert pace.in_use(fake_run(calm)) == ""                                   # Scribe Bot's own work and Docker skipped
    busy = dict(calm, ps="  0 180.0 node\n  0  60.0 pytest\n")
    assert pace.in_use(fake_run(busy)) == "other apps using 2.4 of 8 cores"
    low = dict(calm, memory_pressure="System-wide memory free percentage: 12%")
    assert pace.in_use(fake_run(low)) == "only 12% memory free"
    assert pace.parse_mem_free("nothing") is None


def test_auto_pauses_when_other_apps_use_most_of_the_mac(tmp_path):
    c = conn_(tmp_path)
    pace._state.update(busy_seen=None, busy_calm=None)
    t = 1_000_000.0
    assert pace.busy_step(c, 700.0, t) is None                               # one busy check isn't enough
    assert pace.busy_step(c, 700.0, t + 60) == "paused"
    assert queue.hold_requested(c)                                           # stops at the next safe point
    assert pace.busy_step(c, 300.0, t + 120) is None                         # calm, but not for 2 min yet
    assert pace.busy_step(c, 300.0, t + 250) == "resumed"
    assert not queue.hold_requested(c)
    assert pace.busy_step(c, 700.0, t + 300) is None and pace.busy_step(c, 100.0, t + 360) is None   # a blip: no pause


def test_auto_goes_half_while_busy_and_back_after_3_min(tmp_path):
    c = conn_(tmp_path)
    t = 1_000_000.0
    assert pace.threads_for(c, "", t) == 4
    assert pace.threads_for(c, "other apps using 2.4 of 8 cores", t) == 2
    assert pace.threads_for(c, "", t + 2 * 60) == 2
    assert pace.threads_for(c, "", t + 4 * 60) == 4
    pace.set_mode(c, "full")
    assert pace.threads_for(c, "busy", t) == 4
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

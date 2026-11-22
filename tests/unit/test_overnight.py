"""Overnight backlog + busy-chat rule (Brian, 2026-11-22: "run through as many recordings as it can every night";
an open Claude chat only slows processing when it's doing heavy work). Synthetic data only."""
import json
import time
from datetime import datetime
from types import SimpleNamespace

from scribe_bot.core import db, queue
from scribe_bot.pipeline import control, pace
from test_round5 import _waiting
from test_web_pages import env, one  # noqa: F401

NIGHT = datetime.now().astimezone().replace(hour=2, minute=0)
DAY = datetime.now().astimezone().replace(hour=14, minute=0)


def test_overnight_sends_oldest_one_at_a_time_transcript_only(env, monkeypatch):
    _, conn, _ = env
    for rid in (83, 81, 82):
        _waiting(conn, rid)
    sched = one(conn, "SELECT id FROM review_item WHERE ref_id = 81")
    conn.execute("UPDATE review_item SET payload = ? WHERE id = ?", (json.dumps({"process_at": "2099-01-01T06:00:00+00:00"}), sched))
    sent = []
    monkeypatch.setattr("scribe_bot.pipeline.ingest.approve",
                        lambda c, rec, ai=True, meeting_type=None: (sent.append((rec, ai)), queue.enqueue(c, "recording", rec, {})))
    assert control.overnight_next(conn, DAY) is None and not sent                    # daytime: nothing
    assert control.overnight_next(conn, NIGHT) == 82 and sent == [(82, False)]       # oldest, skips the Process at one
    assert one(conn, "SELECT status FROM review_item WHERE ref_id = 82") == "done"
    assert control.overnight_next(conn, NIGHT) is None                               # one at a time
    conn.execute("UPDATE job SET status = 'done'")
    assert control.overnight_next(conn, NIGHT) == 83
    conn.execute("UPDATE job SET status = 'done'")
    assert control.overnight_next(conn, NIGHT) is None                               # only the Process at one is left
    pace._set(conn, "heat_paused", "1")
    conn.execute("UPDATE review_item SET payload = '{}' WHERE id = ?", (sched,))
    assert control.overnight_next(conn, NIGHT) is None                               # heat guard wins


def fake_run(outputs):
    def run(args, **_):
        return SimpleNamespace(stdout=outputs.get(args[0], ""))
    return run


def test_chat_counts_only_when_the_mac_is_busy_day_or_night(tmp_path):
    chat = tmp_path / ".claude/projects/x"
    chat.mkdir(parents=True)
    (chat / "s.jsonl").write_text("{}")
    idle = '"HIDIdleTime" = 999000000000'
    calm = {"ioreg": idle, "ps": "  0  12.0 /usr/bin/claude\n 10 390.0 python\n  0 300.0 com.docker.virtualization\n"}
    busy = {"ioreg": idle, "ps": "  0 180.0 node\n  0  30.0 claude\n"}
    assert pace.in_use(fake_run(calm), home=tmp_path) == ""            # chat open, Mac calm: full speed
    assert pace.in_use(fake_run(busy), home=tmp_path) == "a busy Claude chat"
    assert pace.parse_busy(busy["ps"]) == 210.0 and pace.parse_busy(calm["ps"]) == 12.0


def test_warm_steps_down_to_half_before_any_pause(env):
    _, conn, _ = env
    pace._state.update(warm_seen=None, warm_cool=None, hot_seen=None, cool_since=None, last_use=0.0)
    pace.set_mode(conn, "full")
    t = 1_000_000.0
    assert pace.heat_step(conn, 91.0, t) is None and pace.threads_for(conn, "", t) == 4      # one warm reading: no change
    assert pace.heat_step(conn, 91.0, t + 301) == "warm" and pace.threads_for(conn, "", t + 301) == 2
    assert not pace.heat_paused(conn)
    assert pace.heat_step(conn, 85.0, t + 400) is None and pace.threads_for(conn, "", t + 400) == 2   # 80-90: stays half
    pace.heat_step(conn, 75.0, t + 500)
    assert pace.heat_step(conn, 75.0, t + 801) == "cool" and pace.threads_for(conn, "", t + 801) == 4
    pace.heat_step(conn, 96.0, t + 900); pace.heat_step(conn, 96.0, t + 1201)                 # still hotter: pause
    assert pace.heat_paused(conn) and pace.threads_for(conn, "", t + 1201) == 2

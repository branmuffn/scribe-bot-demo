"""T1: a quick title + preview from the first 2 minutes of each waiting recording; a Recorder .txt (T2) wins."""
from pathlib import Path

from scribe_bot.pipeline import peek, speech
from test_web_pages import env, one  # noqa: F401


class FakeEngine:
    def to_wav(self, src, dst):
        Path(dst).write_bytes(b"")

    def load(self, wav):
        return list(range(10 * speech.SAMPLE_RATE * 60))   # "10 minutes"

    def transcribe(self, samples):
        assert len(samples) == peek.PEEK_SECONDS * speech.SAMPLE_RATE   # only the first 2 minutes
        return [{"text": "The venue budget is fine. The venue holds the kickoff room."}]


def waiting(conn, tmp: Path, rid: int, name: str) -> None:
    f = tmp / f"{name}.m4a"
    f.write_bytes(b"x")
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                 "VALUES (?, 'recorder', ?, ?, 600, ?, 'awaiting_approval', ?)", (rid, str(f), f"s{rid}", f"2026-11-{rid - 60:02d}T10:00:00", name))
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                 (rid, f'{{"recording_id": {rid}, "title": "{name}", "minutes": 10}}', "2026-11-20T10:00:00"))
    conn.commit()


def test_peek_makes_quick_title_and_txt_wins(env, tmp_path):
    client, conn, _ = env
    speech.set_engine(FakeEngine())
    waiting(conn, tmp_path, 71, "Nov 11 at 09_00")
    waiting(conn, tmp_path, 72, "Nov 12 at 09_00")
    peek._last = -1e9
    assert peek.run_one(conn, child=False, now=0) == 72           # newest first
    assert peek.run_one(conn, child=False, now=10) is None        # waits PEEK_GAP_S before the next one
    assert peek.run_one(conn, child=False, now=10 + peek.PEEK_GAP_S) == 71
    assert peek.run_one(conn, child=False, now=1e6) is None       # all done: past recordings caught up
    page = client.get("/inbox").text
    assert "“Venue" in page and "first 2 min" in page and "The venue budget is fine" in page
    (tmp_path / "Nov 12 at 09_00.txt").write_text("[Speaker 1]\nParking lot parking lot permits permits.\n")
    page = client.get("/inbox").text                              # T2: the whole Recorder transcript wins for that one
    assert "“Parking, permits" in page and "/transcript\">" in page


def test_unreadable_file_is_marked_not_retried(env, tmp_path):
    _, conn, _ = env
    waiting(conn, tmp_path, 73, "Nov 13 at 09_00")
    (tmp_path / "Nov 13 at 09_00.m4a").unlink()
    peek._last = -1e9
    conn.execute("INSERT INTO recording_peek (recording_id, text, made_at) SELECT id, 'x', 'now' FROM recording WHERE id != 73")
    assert peek.run_one(conn, child=False, now=0) == 73
    assert "not found" in one(conn, "SELECT error FROM recording_peek WHERE recording_id = 73")
    assert peek.next_recording(conn) is None


class NoSpeech:
    def __getattr__(self, name):
        raise AssertionError("no speech work when a transcript already exists")


def test_existing_transcript_means_no_speech_work(env, tmp_path):
    """S1 (Brian, 2026-11-20): a recording that comes with a transcript is titled from it, not by the M14."""
    client, conn, _ = env
    speech.set_engine(NoSpeech())
    waiting(conn, tmp_path, 74, "Nov 14 at 09_00")
    (tmp_path / "Nov 14 at 09_00.txt").write_text("[Speaker 1]\nGarden fence garden fence quotes.\n")
    conn.execute("INSERT INTO recording_peek (recording_id, text, made_at) SELECT id, 'x', 'now' FROM recording WHERE id != 74")
    peek._last = -1e9
    assert peek.run_one(conn, child=False, now=0) == 74
    assert "Garden fence" in (one(conn, "SELECT coalesce(text, error) FROM recording_peek WHERE recording_id = 74") or "")


def test_quick_titles_switch(env, tmp_path):
    client, conn, _ = env
    speech.set_engine(NoSpeech())
    waiting(conn, tmp_path, 75, "Nov 15 at 09_00")
    r = client.post("/settings/quick-titles", data={"on": "0"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    peek._last = -1e9
    assert peek.run_one(conn, child=False, now=0) is None            # off: no speech work at all
    assert "Turn on" in client.get("/settings/ai").text
    client.post("/settings/quick-titles", data={"on": "1"})
    assert "Turn off" in client.get("/settings/ai").text


def test_add_transcript_lives_in_card_menu(env, tmp_path):
    client, conn, _ = env
    waiting(conn, tmp_path, 76, "Nov 16 at 09_00")
    page = client.get("/inbox").text
    assert 'id="atx-76"' in page and "</svg> Transcript</button>" in page   # in the row's sheet (redesign 2026-11-21)

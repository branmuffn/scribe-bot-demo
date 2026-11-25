import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline_fakes import BRIAN_VOICE, GUEST_VOICE, THIRD_VOICE, FakeEngine, drop_file, segment
from scribe_bot.core import queue, settings
from scribe_bot.core.db import now
from scribe_bot.pipeline import identify, ingest, paths, recording, speech

PIPELINES = {"recording": recording.PIPELINE}


def words(start: int, end: int, word: str) -> list[tuple[float, str]]:
    return [(float(t), word) for t in range(start, end)]


def turns(*spans) -> list[dict]:
    return [{"start": float(a), "end": float(b), "speaker": c} for a, b, c in spans]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path / "data"))
    ingest._seen.clear()

    def ai_not_available(conn, conv_id):  # pipeline tests never reach the real AI layer (tests override as needed)
        raise NotImplementedError

    monkeypatch.setattr("scribe_bot.ai.extract.extract_conversation", ai_not_available)
    yield settings.load()
    speech.set_engine(None)


def seed_brian(conn):
    conn.execute("INSERT INTO voiceprint (person_id, embedding, updated_at) VALUES (1, ?, ?)",
                 (identify.to_blob(BRIAN_VOICE), now()))


def record(conn, cfg, name="Weekly sync.m4a", content=b"synthetic", ai=True):
    """ai=True: as if Brian picked Summarize (new recordings alone are Transcript only since 2026-11-23)."""
    ingest.scan_inbound(conn, cfg)  # baseline (empty folder)
    drop_file(paths.inbound(cfg), name, content)
    [rec_id] = ingest.scan_inbound(conn, cfg)
    if ai:
        conn.execute("UPDATE job SET payload = json_set(payload, '$.ai', json('true')) WHERE recording_id = ?", (rec_id,))
    job = queue.run_next(conn, PIPELINES, cfg.jobs_dir)
    status = conn.execute("SELECT status, error FROM job WHERE id = ?", (job,)).fetchone()
    assert status["status"] == "done", status["error"]
    return conn.execute("SELECT * FROM conversation WHERE recording_id = ?", (rec_id,)).fetchone(), job


def segment_people(conn, conv_id):
    return [(r["idx"], r["person_id"], r["text"].split()[0]) for r in conn.execute(
        "SELECT g.idx, s.person_id, g.text FROM segment g LEFT JOIN speaker s ON s.id = g.speaker_id "
        "WHERE g.conversation_id = ? ORDER BY g.idx", (conv_id,))]


# Regression for the M0 bug: when the second speaker cluster talks first, names must not swap.
def test_names_follow_clusters_when_second_cluster_speaks_first(conn, cfg):
    seed_brian(conn)
    speech.set_engine(FakeEngine(
        segments=[segment(words(1, 19, "guest") + words(21, 39, "brian") + words(41, 59, "guest"))],
        auto_turns=turns((0, 20, 1), (20, 40, 0), (40, 60, 1)),
        voices={0: BRIAN_VOICE, 1: GUEST_VOICE},
    ))
    conv, _ = record(conn, cfg)
    assert segment_people(conn, conv["id"]) == [(0, None, "guest"), (1, 1, "brian"), (2, None, "guest")]
    assert conv["type"] == "1:1"


def test_full_pipeline_outputs(conn, cfg, tmp_path):
    seed_brian(conn)
    eng = FakeEngine(segments=[segment(words(1, 30, "budget") + words(31, 60, "timeline"))],
                     auto_turns=turns((0, 30, 0), (30, 60, 1)), voices={0: BRIAN_VOICE, 1: GUEST_VOICE})
    speech.set_engine(eng)
    conv, job = record(conn, cfg)
    assert conv["status"] == "transcribed"  # AI layer unavailable in this test
    assert conv["private"] == 0 and conv["title"] == "Weekly sync"

    # Search finds the transcript
    hits = conn.execute("SELECT count(*) FROM search_index WHERE search_index MATCH 'timeline'").fetchone()[0]
    assert hits == 1
    # Markdown export, Opus archive, temp WAV gone
    assert [f.parent.name for f in paths.export(cfg).rglob("*.md")] == ["conversations"]   # one file, where Trash cleans it
    rec = conn.execute("SELECT * FROM recording WHERE id = ?", (conv["recording_id"],)).fetchone()
    assert Path(rec["archive_path"]).exists() and rec["status"] == "done"
    assert not (cfg.jobs_dir / str(job) / "audio16k.wav").exists()
    # Unknown guest -> review queue with a clip; ready ping has title and count only
    item = conn.execute("SELECT * FROM review_item WHERE kind = 'speaker'").fetchone()
    assert Path(json.loads(item["payload"])["clip"]).exists()
    started = conn.execute("SELECT title FROM notification WHERE link LIKE '/?job=%'").fetchall()
    assert started == []   # Brian 2026-11-21: no "Processing started" note
    note = conn.execute("SELECT * FROM notification WHERE link LIKE '/conversations/%'").fetchone()
    assert note["title"].startswith("Ready: Weekly sync · 1 speaker to name · took ")
    assert note["link"] == f"/conversations/{conv['id']}"
    stage = conn.execute("SELECT output FROM stage_run WHERE job_id = ? AND stage = 'ai_extract'", (job,)).fetchone()
    assert stage[0] == "not_available"


def test_transcript_only_skips_the_ai_without_locking(conn, cfg, monkeypatch):
    def boom(*a):
        raise AssertionError("Transcript Only (No AI) must not call the AI")
    monkeypatch.setattr("scribe_bot.ai.extract.extract_conversation", boom)
    conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('approval_mode', 'ask')")
    speech.set_engine(FakeEngine([segment(words(1, 20, "hello"))], turns((0, 20, 0)), {0: GUEST_VOICE}))
    ingest.scan_inbound(conn, cfg)
    drop_file(paths.inbound(cfg), "Oct 3 at 14_05.m4a", b"synthetic")
    [rec_id] = ingest.scan_inbound(conn, cfg)
    job = ingest.approve(conn, rec_id, ai=False)
    assert queue.run_next(conn, PIPELINES, cfg.jobs_dir) == job
    conv = conn.execute("SELECT * FROM conversation WHERE recording_id = ?", (rec_id,)).fetchone()
    rec = conn.execute("SELECT * FROM recording WHERE id = ?", (rec_id,)).fetchone()
    assert conv["private"] == 0 and rec["private"] == 0  # round 7: Make AI notes works later
    # a date-only Recorder name and no AI title: the transcript's quick title (Brian, 2026-11-21), else "Conversation"
    assert conv["title"] == (recording.transcript_title(conn, conv["id"]) or "Conversation") != ""
    stage = conn.execute("SELECT output FROM stage_run WHERE job_id = ? AND stage = 'ai_extract'", (job,)).fetchone()
    assert stage[0] == "skipped_transcript_only"


def test_fallback_title_drops_the_recorder_date(conn, cfg):
    speech.set_engine(FakeEngine([segment(words(1, 20, "hello"))], turns((0, 20, 0)), {0: GUEST_VOICE}))
    conv, _ = record(conn, cfg, name="Weekly sync - Oct 3 at 14_05.m4a")
    assert conv["title"] == "Weekly sync"
    rec = conn.execute("SELECT title FROM recording WHERE id = ?", (conv["recording_id"],)).fetchone()
    assert rec["title"] == "Weekly sync - Oct 3 at 14_05"  # the original name is kept


def test_ai_extract_called_when_available(conn, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr("scribe_bot.ai.extract.extract_conversation",
                        lambda c, cid: calls.append(cid) or {"status": "done", "cost_usd": 0.07})
    speech.set_engine(FakeEngine([segment(words(1, 20, "hello"))], turns((0, 20, 0)), {0: BRIAN_VOICE}))
    conv, _ = record(conn, cfg)
    assert calls == [conv["id"]]


def one_on_one_engine(third_seconds: float) -> FakeEngine:
    end = 190 + third_seconds
    return FakeEngine(
        segments=[segment(words(1, 100, "brian") + words(101, 190, "guest") + words(191, int(end), "echo"))],
        auto_turns=turns((0, 100, 0), (100, 190, 1), (190, end, 2)),
        voices={0: BRIAN_VOICE, 1: GUEST_VOICE, 2: THIRD_VOICE},
        two_turns=turns((0, 100, 0), (100, end, 1)),
        two_voices={0: BRIAN_VOICE, 1: GUEST_VOICE},
    )


def test_one_on_one_rule_folds_into_two_without_a_second_separation(conn, cfg):
    seed_brian(conn)
    eng = one_on_one_engine(third_seconds=9)  # Brian + guest = 190/199 = 95.5%
    speech.set_engine(eng)
    conv, job = record(conn, cfg)
    assert eng.diarize_calls == [None]  # fast path: no second full speaker separation
    out = json.loads((cfg.jobs_dir / str(job) / "identify.json").read_text())
    assert out["auto_speakers"] == 3 and out["one_on_one_rerun"] and out["speakers"] == 2
    assert conv["type"] == "1:1"
    merged = json.loads((cfg.jobs_dir / str(job) / "speakers.json").read_text())
    assert {t["speaker"] for t in merged} == {0, 1}
    assert merged[-1]["speaker"] == 1  # the third voice sounds like neither, so it joins its neighbour in time


def test_merge_to_two_follows_a_clear_voice_match():
    t = turns((0, 100, 0), (100, 190, 1), (190, 199, 2))
    talk = {0: 100.0, 1: 90.0, 2: 9.0}
    embs = {0: BRIAN_VOICE, 1: GUEST_VOICE, 2: [0.96, 0.28, 0.0]}  # clearly Brian, though next to the guest's turn
    assert [x["speaker"] for x in identify.merge_to_two(t, talk, embs, me=0)] == [0, 1, 0]


def test_merge_to_two_keeps_cluster_zero_as_a_real_target():
    # Brian is cluster 1 here; the other main speaker is cluster 0 (a falsy number must still work).
    t = turns((0, 90, 0), (90, 190, 1), (190, 199, 3))
    embs = {0: GUEST_VOICE, 1: BRIAN_VOICE, 3: [0.0, 0.0, 1.0]}
    assert identify.merge_to_two(t, {0: 90.0, 1: 100.0, 3: 9.0}, embs, me=1)[-1]["speaker"] == 1
    embs[3] = [0.28, 0.96, 0.0]  # clearly the guest (cluster 0)
    assert identify.merge_to_two(t, {0: 90.0, 1: 100.0, 3: 9.0}, embs, me=1)[-1]["speaker"] == 0


def test_one_on_one_rule_leaves_real_group_calls(conn, cfg):
    seed_brian(conn)
    eng = one_on_one_engine(third_seconds=20)  # 190/210 = 90%
    speech.set_engine(eng)
    conv, _ = record(conn, cfg)
    assert eng.diarize_calls == [None]
    assert conv["type"] == "multi"


def test_one_on_one_rule_needs_brians_voice(conn, cfg):
    eng = one_on_one_engine(third_seconds=9)  # no voiceprint for Brian yet
    speech.set_engine(eng)
    record(conn, cfg)
    assert eng.diarize_calls == [None]


# M2 "done when": naming a speaker once makes them recognized in the next recording.
def test_naming_a_speaker_once_recognises_them_next_time(conn, cfg):
    seed_brian(conn)
    speech.set_engine(FakeEngine([segment(words(1, 30, "brian") + words(31, 60, "sam"))],
                                 turns((0, 30, 0), (30, 60, 1)), {0: BRIAN_VOICE, 1: GUEST_VOICE}))
    conv, _ = record(conn, cfg, "first.m4a", b"one")
    sid = conn.execute("SELECT id FROM speaker WHERE conversation_id = ? AND person_id IS NULL", (conv["id"],)).fetchone()[0]
    sam = identify.enroll(conn, sid, name="Sam", cfg=cfg)
    assert conn.execute("SELECT status FROM review_item WHERE ref_id = ?", (sid,)).fetchone()[0] == "done"

    # Next recording: Sam's voice is cluster 0 this time and speaks first
    speech.set_engine(FakeEngine([segment(words(1, 30, "sam") + words(31, 60, "brian"))],
                                 turns((0, 30, 0), (30, 60, 1)), {0: GUEST_VOICE, 1: BRIAN_VOICE}))
    drop_file(paths.inbound(cfg), "second.m4a", b"two")
    ingest.scan_inbound(conn, cfg)
    queue.run_next(conn, PIPELINES, cfg.jobs_dir)
    conv2 = conn.execute("SELECT id FROM conversation ORDER BY id DESC").fetchone()[0]
    assert segment_people(conn, conv2) == [(0, sam, "sam"), (1, 1, "brian")]
    assert conn.execute("SELECT count(*) FROM review_item WHERE conversation_id = ? AND status = 'open'",
                        (conv2,)).fetchone()[0] == 0


def test_identify_is_safe_to_rerun(conn, cfg):
    speech.set_engine(FakeEngine([segment(words(1, 30, "a") + words(31, 60, "b"))],
                                 turns((0, 30, 0), (30, 60, 1)), {0: BRIAN_VOICE, 1: GUEST_VOICE}))
    conv, job = record(conn, cfg)
    rerun = recording.rerun(conn, conv["id"], speakers=2)
    queue.run_next(conn, PIPELINES, cfg.jobs_dir)
    assert conn.execute("SELECT status FROM job WHERE id = ?", (rerun,)).fetchone()[0] == "done"
    assert conn.execute("SELECT count(*) FROM speaker WHERE conversation_id = ?", (conv["id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM segment WHERE conversation_id = ?", (conv["id"],)).fetchone()[0] == 2
    assert speech.engine().diarize_calls == [None, 2]  # the re-run forced 2 speakers


# M2 "done when": reconcile catches a deliberately skipped file.
def test_reconcile_catches_a_skipped_file(conn, cfg):
    ingest.scan_inbound(conn, cfg)
    p = drop_file(paths.inbound(cfg), "missed.m4a", b"missed")
    st = p.stat()
    ingest._seen[str(p)] = (st.st_size, st.st_mtime_ns)  # the watcher "skipped" it
    assert ingest.scan_inbound(conn, cfg) == []
    assert recording.reconcile(conn, cfg) == {"new": 1, "requeued": 0}
    assert conn.execute("SELECT count(*) FROM job WHERE kind = 'recording'").fetchone()[0] == 1
    assert "Reconcile found 1 missed recording" in conn.execute("SELECT title FROM notification").fetchone()[0]


def test_cleanup_deletes_original_after_30_days_only_with_archive(conn, cfg, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    for i, has_archive in enumerate([True, False]):
        vault = tmp_path / f"orig{i}.m4a"
        vault.write_bytes(b"x")
        arch = tmp_path / f"arch{i}.caf"
        if has_archive:
            arch.write_bytes(b"opus")
        conn.execute("INSERT INTO recording (source, original_path, sha256, received_at, vault_path, archive_path) "
                     "VALUES ('recorder', 'x', ?, ?, ?, ?)", (f"h{i}", old, str(vault), str(arch)))
    assert recording.cleanup_originals(conn, cfg) == 1
    assert not (tmp_path / "orig0.m4a").exists() and (tmp_path / "orig1.m4a").exists()


def test_schedules_registered():
    names = [s.name for s in recording.make_schedules(None)]
    assert names == ["reconcile", "original_cleanup"]


def test_in_child_runs_in_another_process_and_raises_its_errors():
    """Speaker separation runs this way so the worker's heartbeat keeps beating (2026-10-20 false alerts)."""
    import os
    assert speech.in_child(os.getpid) != os.getpid()
    with pytest.raises(ValueError):
        speech.in_child(int, "not a number")


def test_long_lines_split_at_sentence_ends():
    from scribe_bot.pipeline.lines import speaker_lines
    words = [(" One.", 0), (" Two", 6), (" three.", 13), (" Four", 14), (" five", 20), (" six.", 27), (" Seven", 31)]
    seg = {"tokens": [w for w, _ in words], "token_times": [t for _, t in words]}
    lines = speaker_lines([seg], [{"start": 0, "end": 40, "speaker": 0}])
    assert [x["text"] for x in lines] == ["One. Two three.", "Four five six.", "Seven"]   # never mid-sentence

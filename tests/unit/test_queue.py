import subprocess
import sys
import textwrap
from pathlib import Path

from scribe_bot.core import db, queue

REPO = Path(__file__).resolve().parents[2]


def counting_pipeline(log: list[str], fail_b_times: int = 0) -> queue.Pipeline:
    state = {"b_failures": 0}

    def a(ctx):
        log.append("a")
        out = ctx.work_dir / "a.txt"
        out.write_text("from a")
        return str(out)

    def b(ctx):
        log.append("b")
        if state["b_failures"] < fail_b_times:
            state["b_failures"] += 1
            raise ValueError("synthetic failure")
        assert Path(ctx.outputs["a"]).read_text() == "from a"  # earlier output is handed on
        return "b-done"

    return [("a", a), ("b", b)]


def stages(conn, job_id):
    return {r["stage"]: (r["status"], r["attempts"]) for r in conn.execute(
        "SELECT stage, status, attempts FROM stage_run WHERE job_id = ?", (job_id,))}


def job_status(conn, job_id):
    return conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()[0]


def test_empty_queue(conn, tmp_path):
    assert queue.run_next(conn, {}, tmp_path) is None


def test_runs_all_stages_in_order(conn, tmp_path):
    log = []
    job = queue.enqueue(conn, "demo", payload={"x": 1})
    assert queue.run_next(conn, {"demo": counting_pipeline(log)}, tmp_path) == job
    assert log == ["a", "b"]
    assert job_status(conn, job) == "done"
    assert stages(conn, job) == {"a": ("done", 1), "b": ("done", 1)}


def test_failed_stage_retries_without_rerunning_done_stages(conn, tmp_path):
    log = []
    pipelines = {"demo": counting_pipeline(log, fail_b_times=1)}
    job = queue.enqueue(conn, "demo")
    queue.run_next(conn, pipelines, tmp_path)
    assert job_status(conn, job) == "pending"
    queue.run_next(conn, pipelines, tmp_path)
    assert log == ["a", "b", "b"]
    assert job_status(conn, job) == "done"
    assert stages(conn, job)["b"] == ("done", 2)


def test_gives_up_after_max_attempts(conn, tmp_path):
    pipelines = {"demo": counting_pipeline([], fail_b_times=99)}
    job = queue.enqueue(conn, "demo")
    for _ in range(queue.MAX_ATTEMPTS):
        queue.run_next(conn, pipelines, tmp_path)
    assert job_status(conn, job) == "failed"
    assert queue.run_next(conn, pipelines, tmp_path) is None  # failed jobs are not picked up again


def test_unknown_kind_fails_cleanly(conn, tmp_path):
    job = queue.enqueue(conn, "nope")
    queue.run_next(conn, {}, tmp_path)
    assert job_status(conn, job) == "failed"


def test_jobs_run_oldest_first(conn, tmp_path):
    seen = []
    pipelines = {"demo": [("only", lambda ctx: seen.append(ctx.payload["n"]))]}
    for n in range(3):
        queue.enqueue(conn, "demo", payload={"n": n})
    queue.run_all(conn, pipelines, tmp_path)
    assert seen == [0, 1, 2]


# M1 "done when": a fake multi-stage job survives a forced kill and resumes at the right stage.
WORKER = textwrap.dedent("""
    import os, signal, sys
    from pathlib import Path
    from scribe_bot.core import db, queue

    root = Path(sys.argv[1])
    def a(ctx):
        with open(root / "a_runs", "a") as f: f.write("x")
        return "a-out"
    def b(ctx):
        with open(root / "b_runs", "a") as f: f.write("x")
        os.kill(os.getpid(), signal.SIGKILL)  # like pulling the power mid-stage
    conn = db.connect(root / "test.db")
    queue.run_next(conn, {"demo": [("a", a), ("b", b), ("c", lambda ctx: "c-out")]}, root / "jobs")
""")


def test_survives_forced_kill_and_resumes_at_right_stage(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.migrate(conn)
    job = queue.enqueue(conn, "demo")

    proc = subprocess.run([sys.executable, "-c", WORKER, str(tmp_path)], cwd=REPO, env={"PYTHONPATH": str(REPO)})
    assert proc.returncode == -9  # really killed
    assert job_status(conn, job) == "running"
    assert stages(conn, job) == {"a": ("done", 1), "b": ("running", 1)}

    # Restart: a fresh worker picks the job up and resumes at b, without re-running a.
    log = []
    resumed = {"demo": [("a", lambda ctx: log.append("a")), ("b", lambda ctx: log.append("b") or "b-out"),
                        ("c", lambda ctx: log.append("c") or "c-out")]}
    assert queue.run_next(conn, resumed, tmp_path / "jobs") == job
    assert log == ["b", "c"]
    assert (tmp_path / "a_runs").read_text() == "x"
    assert job_status(conn, job) == "done"
    assert stages(conn, job) == {"a": ("done", 1), "b": ("done", 2), "c": ("done", 1)}

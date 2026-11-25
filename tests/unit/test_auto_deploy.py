"""scripts/auto_deploy.sh (Brian, 2026-11-25), tried for real against a fake M14: a temp HOME with its own
~/scribe-bot clone of a local "GitHub" repo, ~/scribe-bot-data, a stand-in for deploy.sh and for the heat watcher.
Nothing real is touched."""
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/auto_deploy.sh"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# Stand-in for deploy.sh: records its argument, then moves the clone to that commit (or fails like a rollback).
DEPLOY = """#!/bin/bash
echo "$1" >> "$HOME/deploy_calls"
if [ -f "$HOME/deploy_fails" ]; then echo "[deploy] ROLLED BACK to old; the M14 is healthy on the old version"; exit 1; fi
git -C "$HOME/scribe-bot" fetch -q origin && git -C "$HOME/scribe-bot" reset -q --hard "$1"
echo "[deploy] deployed; health check passed"
"""


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, name):
    (repo / name).write_text(name)
    git("add", name, cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", name, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


class M14:
    def __init__(self, tmp):
        self.home = tmp / "home"
        self.data = self.home / "scribe-bot-data"
        (self.data / "logs").mkdir(parents=True)
        self.github = tmp / "github"
        self.github.mkdir()
        git("init", "-q", "-b", "main", cwd=self.github)
        self.first = commit(self.github, "a")
        git("clone", "-q", str(self.github), str(self.home / "scribe-bot"), cwd=tmp)
        self.deploy = tmp / "deploy.sh"
        self.deploy.write_text(DEPLOY)
        self.heat = tmp / "m14"

    def run(self, **extra):
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "SCRIBE_BOT_DATA": str(self.data),
               "AUTO_DEPLOY_CMD": str(self.deploy), "AUTO_DEPLOY_HEAT_TOOL": str(self.heat), **extra}
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        f = self.home / "deploy_calls"
        return f.read_text().split() if f.exists() else []

    def log(self):
        f = self.data / "logs/auto-deploy.log"
        return f.read_text() if f.exists() else ""

    def heat_says(self, text):
        self.heat.write_text(f"#!/bin/bash\ncat <<'X'\n{text}\nX\n")
        self.heat.chmod(0o755)

    def job(self, status):
        conn = sqlite3.connect(self.data / "scribe-bot.db")
        conn.execute("CREATE TABLE IF NOT EXISTS job (id INTEGER PRIMARY KEY, kind TEXT, status TEXT)")
        conn.execute("INSERT INTO job (kind, status) VALUES ('recording', ?)", (status,))
        conn.commit()
        conn.close()


@pytest.fixture
def m14(tmp_path):
    return M14(tmp_path)


def test_nothing_new_does_nothing_and_stays_quiet(m14):
    started = time.monotonic()
    r = m14.run()
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - started < 5          # the idle check is quick (well under a second on its own)
    # R4: the very first check writes one "on" line so the log exists; after that, "nothing new" stays quiet
    first = m14.log()
    assert m14.calls() == [] and first.count("\n") == 1 and " on: checking every 5 minutes" in first
    assert (m14.data / "auto-deploy/last-check").exists()
    assert m14.run().returncode == 0 and m14.log() == first   # no log line for "nothing new", every 5 minutes


def test_a_new_commit_on_main_is_deployed(m14):
    new = commit(m14.github, "b")
    assert m14.run().returncode == 0
    assert m14.calls() == [new]
    assert f"deploy {new[:7]}" in m14.log() and f"deployed {new[:7]}: health check passed" in m14.log()
    m14.run()                                       # now it's on that commit: nothing more happens
    assert m14.calls() == [new]


def test_waits_while_a_recording_is_processing_then_deploys(m14):
    new = commit(m14.github, "b")
    m14.job("running")
    m14.run()
    m14.run()
    assert m14.calls() == []
    assert m14.log().count("a recording is being processed") == 1   # said once, not every 5 minutes
    conn = sqlite3.connect(m14.data / "scribe-bot.db")
    conn.execute("UPDATE job SET status = 'done'")
    conn.commit()
    conn.close()
    m14.job("pending")                              # waiting jobs don't block: a restart doesn't lose them
    m14.run()
    assert m14.calls() == [new]


# The real output of `m14 status` (Brian, 2026-11-25), and versions of it.
SAMPLE = """M14 now · 11:33 AM
🌡 Heat 80 °C (normal under 85)
⚡ Speed 100% (full speed)
🌀 Fans 3,158 and 2,951 rpm (two fans)
↳ last 5 min: average 65 °C, peak 72 °C (the number above is this second's hottest core; it swings 10+ °C in seconds when something is busy)
🧠 Memory 75% free · swap 0.6 GB
💾 Storage 62% used
🔋 Battery 48% (plugged in)
🔥 Busiest: spotlightknowledged 100% (100% = one core of 8)."""


@pytest.mark.parametrize("status", [
    SAMPLE.replace("average 65 °C", "average 91 °C"),
    SAMPLE.replace("Speed 100% (full speed)", "Speed 80% (slowed)"),
    "🌡 Heat 92 °C\n⚡ Speed 100%",                 # no 5-minute line: the Heat line counts
])
def test_waits_while_the_m14_is_hot(m14, status):
    commit(m14.github, "b")
    m14.heat_says(status)
    m14.run()
    assert m14.calls() == [] and "the M14 is hot" in m14.log()


@pytest.mark.parametrize("status", [
    None,                                            # the watcher isn't installed
    SAMPLE,                                          # 80 °C now, 65 °C average, full speed
    SAMPLE.replace("Heat 80 °C", "Heat 95 °C"),      # a spike this second, but the 5-minute average is fine
    "something it can't read",
])
def test_carries_on_when_cool_or_the_watcher_is_missing(m14, status):
    if status is not None:
        m14.heat_says(status)
    new = commit(m14.github, "b")
    m14.run()
    assert m14.calls() == [new]


def test_one_at_a_time(m14):
    commit(m14.github, "b")
    lock = m14.data / "auto-deploy/lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))     # a live process holds the lock
    m14.run()
    assert m14.calls() == [] and "another auto-deploy is running" in m14.log()
    assert lock.exists()                            # someone else's lock is left alone


def test_a_lock_left_by_a_dead_run_is_taken_over(m14):
    new = commit(m14.github, "b")
    lock = m14.data / "auto-deploy/lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("999999")
    m14.run()
    assert m14.calls() == [new] and not lock.exists()


def test_a_failed_deploy_is_not_retried_until_a_newer_commit(m14):
    bad = commit(m14.github, "b")
    (m14.home / "deploy_fails").touch()
    m14.run()
    m14.run()
    assert m14.calls() == [bad]                     # no restart loop every 5 minutes
    assert f"FAILED {bad[:7]}" in m14.log() and "ROLLED BACK" in m14.log()
    (m14.home / "deploy_fails").unlink()
    good = commit(m14.github, "c")
    m14.run()
    assert m14.calls() == [bad, good]


def test_offline_is_logged_once(m14):
    git("remote", "set-url", "origin", str(m14.home / "nowhere"), cwd=m14.home / "scribe-bot")
    m14.run()
    m14.run()
    assert m14.calls() == [] and m14.log().count("couldn't reach GitHub") == 1


def test_the_script_never_uses_sudo_or_stops_processes():
    body = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert "sudo" not in body and "pkill" not in body and "killall" not in body
    assert "security " not in body                  # never reads the Keychain
    for line in body.splitlines():
        if "kill " in line:
            assert "kill -0" in line                # only asks whether the lock's owner is alive


def test_launchd_job_runs_every_five_minutes_at_low_priority():
    plist = (REPO / "scripts/launchd/com.scribebot.autodeploy.plist.template").read_text()
    assert "<key>StartInterval</key><integer>300</integer>" in plist
    assert "<key>Nice</key><integer>10</integer>" in plist and "LowPriorityIO" in plist


def test_holds_after_a_rollback_by_hand_until_a_newer_commit(m14):
    main_now = commit(m14.github, "b")
    (m14.data / "auto-deploy").mkdir(exist_ok=True)
    (m14.data / "auto-deploy/hold").write_text(main_now + "\n")   # what rollback.sh writes
    m14.run()
    m14.run()
    assert m14.calls() == [] and m14.log().count("rolled back by hand") == 1
    fix = commit(m14.github, "c")
    m14.run()
    assert m14.calls() == [fix]


def test_rollback_sets_the_hold():
    body = (REPO / "scripts/rollback.sh").read_text()
    assert "auto-deploy/hold" in body and "git rev-parse origin/main" in body

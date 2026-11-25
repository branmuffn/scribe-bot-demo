"""deploy.sh run on the M14 itself (Brian, 2026-11-25), tried for real against a fake M14: a temp HOME with its own
~/scribe-bot clone, ~/scribe-bot-data, and stand-ins for launchctl, curl and uv. Nothing real is touched."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

LAUNCHCTL = """#!/bin/bash
[ -f "$HOME/no_services" ] && exit 1
case "$1" in print) echo "state = running" ;; kickstart) echo "$3" >> "$HOME/kicked" ;; esac
"""
CURL = "#!/bin/bash\nexit 0\n"
UV = """#!/bin/bash
case "$*" in
  sync*) exit 0 ;;
  *scribe_bot.migrate*) echo '{"applied": []}' ;;
  *scribe_bot.health*)
    if [ -f "$HOME/bad_commit" ] && [ "$(git -C "$HOME/scribe-bot" rev-parse HEAD)" = "$(cat "$HOME/bad_commit")" ]; then
      echo '{"ok": false}'; exit 1; fi
    echo '{"ok": true}' ;;
esac
"""


def sh(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def m14(tmp_path):
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    (home / "scribe-bot-data/logs").mkdir(parents=True)
    stubs = tmp_path / "stubs"; stubs.mkdir()
    for name, body, where in (("launchctl", LAUNCHCTL, stubs), ("curl", CURL, stubs), ("uv", UV, home / ".local/bin")):
        (where / name).write_text(body); (where / name).chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{stubs}:{os.environ['PATH']}", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for k in ("DRY_RUN", "SCRIBE_DEPLOY", "SCRIBE_BOT_DATA", "DEPLOY_FROM_COPY", "DEPLOY_HERE"):
        env.pop(k, None)
    origin = tmp_path / "origin.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin), env=env)
    work = tmp_path / "work"
    sh("git", "clone", "-q", str(origin), str(work), env=env)
    (work / "scripts").mkdir()
    for f in ("deploy.sh", "rollback.sh", "m14.sh"):
        shutil.copy(REPO / "scripts" / f, work / "scripts" / f)
    (work / "app.txt").write_text("v1\n")
    sh("git", "add", "-A", cwd=work, env=env); sh("git", "commit", "-qm", "v1", cwd=work, env=env)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=work, env=env)
    v1 = sh("git", "rev-parse", "HEAD", cwd=work, env=env)
    sh("git", "clone", "-q", str(origin), str(home / "scribe-bot"), env=env)   # the M14's copy, on v1
    (work / "app.txt").write_text("v2\n")
    sh("git", "commit", "-qam", "v2", cwd=work, env=env); sh("git", "push", "-q", "origin", "HEAD:main", cwd=work, env=env)
    v2 = sh("git", "rev-parse", "HEAD", cwd=work, env=env)
    return home, env, v1, v2


def deploy(home, env, *args, script="deploy.sh", **extra):
    return subprocess.run(["bash", f"scripts/{script}", *args], cwd=home / "scribe-bot", env={**env, **extra},
                          capture_output=True, text=True)


def head(home, env):
    return sh("git", "rev-parse", "HEAD", cwd=home / "scribe-bot", env=env)


def test_deploys_on_the_m14_without_ssh(m14):
    home, env, v1, v2 = m14
    p = deploy(home, env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "mode: local" in p.stdout and p.stdout.strip().endswith("health check passed")
    assert head(home, env) == v2 and (home / "scribe-bot/app.txt").read_text() == "v2\n"
    assert [k.rsplit("/", 1)[1] for k in (home / "kicked").read_text().split()] == ["com.scribebot.worker", "com.scribebot.web"]
    log = (home / "scribe-bot-data/logs/deploys.log").read_text()
    assert f"prev={v1} target={v2} result=ok from=local" in log
    assert not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("scribe-deploy.*"))   # its temporary copy is cleaned up


def test_dry_run_on_the_m14_changes_nothing(m14):
    home, env, v1, v2 = m14
    p = deploy(home, env, DRY_RUN="1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert f"M14 is at {v1[:7]}; deploying {v2[:7]}" in p.stdout                     # real commits, read for real
    assert head(home, env) == v1 and not (home / "kicked").exists()
    assert not (home / "scribe-bot-data/logs/deploys.log").exists()
    lines = p.stdout.strip().splitlines()
    assert lines[0].startswith("[deploy] ==== DRY RUN: nothing will change")
    assert f"DRY RUN finished: nothing changed. A real deploy would move the M14 from {v1[:7]} to {v2[:7]}" in lines[-2]
    assert lines[-1].endswith("bash scripts/deploy.sh") and "health check passed" not in p.stdout


def test_real_deploy_never_says_dry_run(m14):
    home, env, *_ = m14
    p = deploy(home, env)
    assert p.returncode == 0 and "DRY RUN" not in p.stdout


def test_unhealthy_new_version_rolls_back(m14):
    home, env, v1, v2 = m14
    (home / "bad_commit").write_text(v2)
    p = deploy(home, env)
    assert p.returncode == 1
    assert "ROLLED BACK" in p.stdout and head(home, env) == v1
    assert "result=rolled-back from=local" in (home / "scribe-bot-data/logs/deploys.log").read_text()


def test_uncommitted_changes_stop_it_untouched(m14):
    home, env, v1, _ = m14
    (home / "scribe-bot/app.txt").write_text("edited on the M14\n")
    p = deploy(home, env)
    assert p.returncode == 1 and "uncommitted changes" in p.stdout and "app.txt" in p.stdout
    assert head(home, env) == v1 and (home / "scribe-bot/app.txt").read_text() == "edited on the M14\n"
    assert not (home / "kicked").exists()


def test_missing_services_are_never_counted_as_healthy(m14):
    home, env, v1, _ = m14
    (home / "no_services").write_text("")
    p = deploy(home, env)
    assert p.returncode == 1 and "m14_services.sh" in p.stdout and head(home, env) == v1


def test_rollback_on_the_m14_goes_back_to_the_previous_commit(m14):
    home, env, v1, v2 = m14
    assert deploy(home, env).returncode == 0
    p = deploy(home, env, script="rollback.sh")
    assert p.returncode == 0, p.stdout + p.stderr
    assert head(home, env) == v1 and "health check passed" in p.stdout

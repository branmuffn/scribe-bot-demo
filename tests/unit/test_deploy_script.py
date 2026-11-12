import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def dry(script, *args):
    p = subprocess.run(["bash", str(REPO / "scripts" / script), *args], capture_output=True, text=True,
                       env={**os.environ, "DRY_RUN": "1"})
    commands = [line[len("m14$ "):] for line in p.stderr.splitlines() if line.startswith("m14$ ")]
    return p, commands


def test_deploy_dry_run_runs_steps_in_order():
    p, cmds = dry("deploy.sh")
    assert p.returncode == 0, p.stderr
    joined = "\n".join(cmds)
    order = ["git rev-parse HEAD", "git fetch", "git rev-parse --verify origin/main", "git reset -q --keep <target-commit>",
             "uv sync", "scribe_bot.migrate", "launchctl kickstart", "scribe_bot.health",
             "127.0.0.1:8000/health", "state = running", "deploys.log"]
    positions = [joined.index(step) for step in order]
    assert positions == sorted(positions)
    assert "deployed" in p.stdout
    assert "result=ok" in joined


def test_deploy_dry_run_takes_a_target():
    _, cmds = dry("deploy.sh", "abc1234")
    assert any("git rev-parse --verify abc1234^{commit}" in c for c in cmds)


def test_rollback_dry_run_reads_deploy_log_then_deploys():
    p, cmds = dry("rollback.sh")
    assert p.returncode == 0, p.stderr
    assert "deploys.log" in cmds[0]
    assert any("git rev-parse --verify <previous-commit>" in c for c in cmds)

"""Pre-change config snapshot.

Runs before any deploy task. Pulls the device's current running
config via NAPALM `get_config` and writes it to
build/<host>.pre-commit.conf so we always have a known-good baseline
to manually restore from if a deploy goes wrong AND the auto-rollback
also fails.

Cheap insurance: ~1 second per host, runs in parallel.
"""

from pathlib import Path

from nornir.core.task import Result, Task
from nornir_napalm.plugins.tasks import napalm_get


def pre_commit_backup(task: Task, build_dir: Path) -> Result:
    """Snapshot running config to build/<host>.pre-commit.conf."""
    out = task.run(task=napalm_get, getters=["config"])
    running = out.result["config"]["running"]
    snap = build_dir / f"{task.host.name}.pre-commit.conf"
    snap.write_text(running, encoding="utf-8", newline="\n")
    return Result(host=task.host, result=f"snapshot {snap.name} ({len(running)} bytes)")

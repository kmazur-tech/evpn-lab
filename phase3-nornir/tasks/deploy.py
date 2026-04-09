"""NAPALM deploy task for Phase 3.

Two-stage commit-confirmed flow:

Stage 1: napalm_deploy() loads the rendered config and either
  - dry-runs (compare only, no commit)
  - or commits with revert_in=300 (Junos `commit confirmed 5`).
    The device starts a 5-minute auto-rollback timer. If we don't
    confirm within that window, Junos automatically rolls back to
    the previous config. This is the safety net that would have
    prevented the placeholder-hash credential lockout: a deploy
    that breaks management access self-corrects.

Stage 2: napalm_confirm_commit() (called from deploy.py main, NOT
  from this task) sends a no-op confirm to clear the rollback timer
  and finalize the commit. Only called after a liveness check
  proves all devices are still reachable post-deploy.
"""

from pathlib import Path

from nornir.core.task import Result, Task
from nornir_napalm.plugins.tasks import napalm_configure


# Junos `commit confirmed N` timer in seconds. NAPALM Junos driver
# requires multiples of 60. 300s = 5 min = standard short window for
# automated deploys with a fast liveness check.
REVERT_IN_SECONDS = 300


def napalm_deploy(task: Task, build_dir: Path, commit: bool) -> Result:
    """Replace the device's running config with build/<host>.conf.

    When commit=True, uses `revert_in=300` (Junos commit confirmed 5).
    The caller MUST then run a liveness check and call
    napalm_confirm_commit on every host that's still reachable; any
    host whose confirm is skipped will auto-rollback at the deadline.
    """
    config_path = build_dir / f"{task.host.name}.conf"
    config_text = config_path.read_text(encoding="utf-8")

    kwargs = {
        "configuration": config_text,
        "replace": True,
        "dry_run": not commit,
    }
    if commit:
        kwargs["revert_in"] = REVERT_IN_SECONDS

    out = task.run(task=napalm_configure, **kwargs)
    diff = out.result or ""
    if commit:
        label = f"COMMIT-CONFIRMED ({REVERT_IN_SECONDS}s rollback timer)"
    else:
        label = "DRY-RUN"
    if not diff.strip():
        return Result(host=task.host, result=f"{label}: no diff")
    return Result(host=task.host, result=f"{label}:\n{diff}")


def liveness_check(task: Task) -> Result:
    """Cheap post-deploy reachability proof: SSH + 'show version'.

    Used between Stage 1 (commit-confirmed) and Stage 2 (confirm)
    to make sure the new config didn't break management access.
    If this fails on any device, the caller does NOT confirm and
    Junos auto-rolls back at the deadline.
    """
    from nornir_napalm.plugins.tasks import napalm_get

    out = task.run(task=napalm_get, getters=["facts"])
    facts = out.result["facts"]
    return Result(
        host=task.host,
        result=f"alive: hostname={facts.get('hostname')} model={facts.get('model')}",
    )

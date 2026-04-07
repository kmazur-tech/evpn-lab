"""NAPALM deploy task for Phase 3.

Loads the rendered config (build/<host>.conf) onto the device using
NAPALM's load_replace_candidate (= Junos `load override`), runs
`compare_config` to surface what would change, and either commits or
discards based on the `commit` flag.
"""

from pathlib import Path

from nornir.core.task import Result, Task
from nornir_napalm.plugins.tasks import napalm_configure


def napalm_deploy(task: Task, build_dir: Path, commit: bool) -> Result:
    """Replace the device's running config with build/<host>.conf.

    Returns the NAPALM diff in `result`. When `commit=False` the
    candidate is discarded after the compare (`dry_run=True`).
    """
    config_path = build_dir / f"{task.host.name}.conf"
    config_text = config_path.read_text(encoding="utf-8")

    out = task.run(
        task=napalm_configure,
        configuration=config_text,
        replace=True,
        dry_run=not commit,
    )
    diff = out.result or ""
    label = "COMMITTED" if commit else "DRY-RUN"
    if not diff.strip():
        return Result(host=task.host, result=f"{label}: no diff")
    return Result(host=task.host, result=f"{label}:\n{diff}")

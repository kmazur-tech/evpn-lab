"""NAPALM deploy task for Phase 3.

Two layered safety nets:

**Inner gate (mgmt-plane breakage)** -- when napalm_deploy is called
with revert_in set, the commit is `commit confirmed N` and Junos starts
an auto-rollback timer. The caller waits LIVENESS_WAIT_SECONDS, runs
liveness_check on every host, and only then calls napalm_confirm_commit
to clear the timer. If the new config broke SSH on any host (placeholder
hash, mgmt VRF misconfig, etc.), liveness fails, the confirm never
fires, and Junos rolls back at the deadline. Fast (under 2 minutes
total) and fully automatic.

**Outer gate (functional regressions caught by smoke/drift)** -- the
deploy commit also carries a unique log line (the "marker") supplied via
commit_message. If a later post-deploy check (smoke, drift) fails after
the inner gate already cleared, restore_from_marker walks `show system
commit`, finds the entry whose log matches the marker, and rolls back
to the configuration as it was BEFORE that entry.

Why both gates: commit-confirmed is reliable as the inner gate because
it fires BEFORE smoke runs -- nothing else commits to the device in
that window. It is NOT reliable as the outer gate, because smoke's own
failover commits would clear the timer mid-run. The marker walk
survives those intermediate commits.
"""

import os
from pathlib import Path
from typing import Optional

from nornir.core.task import Result, Task
from nornir_napalm.plugins.tasks import napalm_configure


# Inner-gate timing. NAPALM Junos requires revert_in to be a multiple of
# 60 seconds (Junos `commit confirmed N` is minute-granular when N >=
# 60). 120 s gives the orchestrator ~90 s of headroom after the 30 s
# settle wait + liveness RPCs to issue the confirm before Junos rolls
# back unilaterally.
LIVENESS_REVERT_IN_SECONDS = 120

# How long to wait after the commit-confirmed before running the
# liveness probe. Long enough for Junos to finish reapplying interface
# config and re-establish BGP sessions to the mgmt loopback peers, but
# well inside the 120 s revert deadline so we have time left for the
# liveness RPCs and the confirm.
LIVENESS_WAIT_SECONDS = 30


def napalm_deploy(
    task: Task,
    build_dir: Path,
    commit: bool,
    commit_message: Optional[str] = None,
    revert_in: Optional[int] = None,
) -> Result:
    """Replace the device's running config with build/<host>.conf.

    When commit=True, performs a Junos commit. If commit_message is set,
    it becomes the Junos commit comment ("log") which restore_from_marker
    later locates in the commit history. If revert_in is set, the commit
    is `commit confirmed N` and the caller MUST follow up with
    napalm_confirm_commit (or another commit) within N seconds; otherwise
    Junos auto-rolls back. revert_in is the inner gate; the marker is
    the outer gate; they compose.
    """
    config_path = build_dir / f"{task.host.name}.conf"
    config_text = config_path.read_text(encoding="utf-8")

    kwargs = {
        "configuration": config_text,
        "replace": True,
        "dry_run": not commit,
    }
    if commit and commit_message:
        kwargs["commit_message"] = commit_message
    if commit and revert_in is not None:
        kwargs["revert_in"] = revert_in

    out = task.run(task=napalm_configure, **kwargs)
    # napalm_configure returns the diff in the `.diff` attribute, not
    # `.result`. An earlier version of this code read `out.result or ""`,
    # which was always empty (because napalm_configure never sets the
    # `.result` field), so deploy.py printed "no diff" for every commit
    # regardless of what NAPALM was internally doing. This was the root
    # cause of the misleading output around the credential lockout
    # incident - NAPALM was honestly diffing and committing, our display
    # layer just wasn't reading the right attribute.
    inner = out[0] if len(out) else None
    diff = (inner.diff if inner is not None else None) or ""
    if commit:
        marker = f" marker={commit_message!r}" if commit_message else ""
        timer = f" revert_in={revert_in}s" if revert_in is not None else ""
        label = f"COMMIT{marker}{timer}"
    else:
        label = "DRY-RUN"
    if not diff.strip():
        return Result(host=task.host, result=f"{label}: no diff")
    return Result(host=task.host, result=f"{label}:\n{diff}")


def liveness_check(task: Task) -> Result:
    """Cheap post-deploy reachability proof: SSH + 'show version'.

    Used as a quick sanity check after a non-CI deploy. CI uses the
    smoke suite as a stronger gate.
    """
    from nornir_napalm.plugins.tasks import napalm_get

    out = task.run(task=napalm_get, getters=["facts"])
    facts = out.result["facts"]
    return Result(
        host=task.host,
        result=f"alive: hostname={facts.get('hostname')} model={facts.get('model')}",
    )


def restore_from_marker(task: Task, marker: str) -> Result:
    """Find the device commit whose log == marker, then load_replace +
    commit the configuration as it was the commit BEFORE that one.

    Walks `show system commit` (PyEZ get_commit_information RPC) and
    matches by the literal commit comment. The matching entry's
    sequence-number is N; we rollback to rb_id=N+1 (the state before
    that commit) and re-commit with a rollback narrative comment.

    Hard fails if the marker is not found in commit history. We do
    NOT fall back to "rollback 1" - that would mask the real problem
    (e.g. operator manually committed on top of CI's commit) and
    silently rewind the wrong change.
    """
    from jnpr.junos import Device
    from jnpr.junos.utils.config import Config

    user = os.environ.get("JUNOS_SSH_USER")
    pwd = os.environ.get("JUNOS_SSH_PASSWORD")
    if not user or not pwd:
        raise RuntimeError(
            "JUNOS_SSH_USER and JUNOS_SSH_PASSWORD must be set in env "
            "to perform marker-based rollback."
        )

    dev = Device(host=task.host.hostname, user=user, passwd=pwd, port=22)
    dev.open()
    try:
        history = dev.rpc.get_commit_information()
        entries = history.findall("commit-history")
        target_index = None
        seen = []
        for entry in entries:
            seq = (entry.findtext("sequence-number") or "").strip()
            log = (entry.findtext("log") or "").strip()
            seen.append((seq, log))
            if log == marker:
                # rb_id 0 is current. The marker commit IS at seq, so the
                # state BEFORE it is one step further back: seq + 1.
                target_index = int(seq) + 1
                break
        if target_index is None:
            raise RuntimeError(
                f"marker {marker!r} not found in commit history on "
                f"{task.host.name}. Recent entries: {seen[:8]}"
            )

        cfg = Config(dev)
        cfg.rollback(rb_id=target_index)
        cfg.commit(comment=f"automated rollback before {marker}")
        return Result(
            host=task.host,
            result=(
                f"rolled back to commit-index {target_index} "
                f"(state before {marker!r})"
            ),
        )
    finally:
        dev.close()

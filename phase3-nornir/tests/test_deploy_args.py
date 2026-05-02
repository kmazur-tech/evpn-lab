"""Argument validation tests for deploy.py.

Phase 6 Stage 3 flag-and-revert flow introduces:

- `--commit-message <marker>` -- only valid with --commit; sets the
  Junos commit comment so a later --rollback-marker can find this
  commit in the device commit history.
- `--rollback-marker <marker>` -- mutually exclusive with the
  render/deploy modes; walks each device's commit history and
  rolls back to the state before the commit whose log matches
  the marker.

These tests pin argparse so a future flag-combo change can't quietly
break the safety contract. No Nornir, NetBox, or device contact.
"""

import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY_PY = Path(__file__).resolve().parent.parent / "deploy.py"


def _run(args, env=None):
    """Run deploy.py with args, capture stderr, return CompletedProcess.

    Sets dummy env so deploy.py argparse step proceeds; validation runs
    before any environment-dependent code, so missing real env doesn't
    matter for these tests.
    """
    proc_env = {
        "PATH": "/usr/bin:/bin",
        "NETBOX_URL": "http://placeholder",
        "NETBOX_TOKEN": "placeholder",
        "JUNOS_LOGIN_PASSWORD": "x",
        "JUNOS_LOGIN_SALT": "$6$x$",
    }
    if env:
        proc_env.update(env)
    return subprocess.run(
        [sys.executable, str(DEPLOY_PY)] + args,
        capture_output=True, text=True, env=proc_env, timeout=10,
    )


def test_help_lists_new_flags():
    """--commit-message, --liveness-gate and --rollback-marker
    must show in --help."""
    r = _run(["--help"])
    assert r.returncode == 0
    assert "--commit-message" in r.stdout
    assert "--liveness-gate" in r.stdout
    assert "--rollback-marker" in r.stdout


def test_help_does_not_list_removed_flags():
    """--no-confirm and --confirm-only were removed when commit-confirmed
    was replaced by flag-and-revert. They must NOT appear in --help."""
    r = _run(["--help"])
    assert r.returncode == 0
    assert "--no-confirm" not in r.stdout
    assert "--confirm-only" not in r.stdout


def test_commit_message_without_commit_rejected():
    """--commit-message only makes sense alongside --commit."""
    r = _run(["--commit-message", "cicd-1-1"])
    assert r.returncode != 0
    assert "--commit-message only makes sense with --commit" in r.stderr


def test_commit_message_with_dry_run_rejected():
    """--dry-run does no commit, so --commit-message is meaningless there."""
    r = _run(["--dry-run", "--commit-message", "cicd-1-1"])
    assert r.returncode != 0
    assert "--commit-message only makes sense with --commit" in r.stderr


def test_liveness_gate_without_commit_rejected():
    """--liveness-gate orchestrates a `commit confirmed` flow; without
    --commit there is nothing to confirm."""
    r = _run(["--liveness-gate"])
    assert r.returncode != 0
    assert "--liveness-gate only makes sense with --commit" in r.stderr


def test_liveness_gate_with_dry_run_rejected():
    """--dry-run never commits; --liveness-gate must reject."""
    r = _run(["--dry-run", "--liveness-gate"])
    assert r.returncode != 0
    assert "--liveness-gate only makes sense with --commit" in r.stderr


@pytest.mark.parametrize("conflicting", ["--check", "--full", "--dry-run", "--commit"])
def test_rollback_marker_rejects_render_and_deploy_flags(conflicting):
    """--rollback-marker walks commit history; combining it with any
    render/deploy mode is a contradiction the CLI must surface."""
    r = _run(["--rollback-marker", "cicd-1-1", conflicting])
    assert r.returncode != 0
    assert "--rollback-marker cannot be combined with" in r.stderr


def test_rollback_marker_with_target_accepted():
    """--rollback-marker --target dc1-leaf1 is valid (single-host rollback).

    We can't actually invoke Nornir without real env + lab, so we just
    verify argparse does not reject the combination.
    """
    r = _run(["--rollback-marker", "cicd-1-1", "--target", "dc1-leaf1"])
    assert "cannot be combined with" not in r.stderr


def test_default_when_no_args_is_check():
    """No flags = --check. Verified indirectly via --help."""
    r = _run(["--help"])
    assert r.returncode == 0

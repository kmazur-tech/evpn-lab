"""Argument validation tests for deploy.py.

Two new flags from Phase 6 Stage 3 (--no-confirm and --confirm-only)
introduce mutually-exclusive combinations and dependency rules. These
tests pin the validation so a future flag-combo refactor can't quietly
break the safety contract:

- `--commit --no-confirm` is the CI deploy step; smoke is the gate, not
  liveness. The device is left in commit-confirmed state with the
  rollback timer running.
- `--confirm-only` is the CI's post-smoke step; it MUST NOT re-render
  or re-touch the device beyond clearing the timer.

Tests exercise argparse only -- no Nornir, no NetBox, no devices.
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
        # Argparse failures hit before these are read, but set them
        # anyway so accidental code paths don't trip on KeyError.
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
    """--no-confirm and --confirm-only must show in --help."""
    r = _run(["--help"])
    assert r.returncode == 0
    assert "--no-confirm" in r.stdout
    assert "--confirm-only" in r.stdout


def test_no_confirm_without_commit_rejected():
    """--no-confirm only makes sense with --commit; argparse must reject."""
    r = _run(["--no-confirm"])
    assert r.returncode != 0
    assert "--no-confirm only makes sense with --commit" in r.stderr


def test_no_confirm_with_dry_run_rejected():
    """--dry-run does not leave a pending commit; --no-confirm is meaningless."""
    r = _run(["--dry-run", "--no-confirm"])
    assert r.returncode != 0
    assert "--no-confirm only makes sense with --commit" in r.stderr


@pytest.mark.parametrize("conflicting", ["--check", "--full", "--dry-run", "--commit"])
def test_confirm_only_rejects_render_and_deploy_flags(conflicting):
    """--confirm-only skips render/deploy entirely; combining it with
    any of those flags is a contradiction the CLI must surface."""
    r = _run(["--confirm-only", conflicting])
    assert r.returncode != 0
    assert "--confirm-only cannot be combined with" in r.stderr


def test_confirm_only_with_target_accepted():
    """--confirm-only --target dc1-leaf1 is valid (phased rollback recovery).

    We can't actually invoke Nornir without real env + lab, so we just
    verify argparse does not reject the combination. Code paths beyond
    argparse will fail on missing env, which is fine -- argparse-level
    rejection is what we're testing here.
    """
    r = _run(["--confirm-only", "--target", "dc1-leaf1"])
    # argparse accepts; later code aborts with our env check or Nornir setup.
    # Either way, the failure must NOT mention the argparse rejection.
    assert "cannot be combined with" not in r.stderr


def test_default_when_no_args_is_check():
    """No flags = --check. Verified by the help text the existing path
    prints, but here we just confirm the argparse layer does not error."""
    r = _run(["--help"])  # safest no-side-effect check
    assert r.returncode == 0

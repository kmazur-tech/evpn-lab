"""Pin the contract: napalm_configure returns its diff in `.diff`, not `.result`.

The original deploy.py read `out.result or ""` which was always empty
because napalm_configure never sets the `.result` attribute. The bug
hid every NAPALM diff from the operator output for the entire history
of the credential-lockout incident. This test pins the contract so
the bug can't recur silently.

Strategy: monkeypatch nornir_napalm's napalm_configure to return a
known Result with diff='[edit] sample diff', call our napalm_deploy
task via Nornir's task runner, and verify the displayed string
contains the diff text.
"""
from pathlib import Path

import pytest
from nornir.core.task import Result, Task

from tasks.deploy import napalm_deploy


class _FakeMultiResult(list):
    """Mimic Nornir's MultiResult enough for our tests."""
    pass


class _FakeTask:
    """Minimal Task stand-in. We only need .host and .run()."""

    def __init__(self, host_name, fake_diff, fake_changed=True):
        self.host = type("H", (), {"name": host_name})()
        self._fake_diff = fake_diff
        self._fake_changed = fake_changed
        self.run_calls = []

    def run(self, task, **kwargs):
        # Capture the call so tests can assert it
        self.run_calls.append((task, kwargs))
        # Return what nornir_napalm.napalm_configure would return:
        # Result(host=..., diff=<diff>, changed=...)
        r = Result(host=self.host, diff=self._fake_diff, changed=self._fake_changed)
        return _FakeMultiResult([r])


def test_napalm_deploy_reads_diff_attribute_not_result(tmp_path):
    """The bug: out.result is always None for napalm_configure;
    must read out[0].diff instead."""
    # Write a dummy build/<host>.conf
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    fake_diff = '[edit interfaces ge-0/0/0]\n-   description "old";\n+   description "new";'
    task = _FakeTask("dc1-spine1", fake_diff=fake_diff)

    result = napalm_deploy(task, build_dir=tmp_path, commit=False)

    # The displayed string MUST contain the diff text
    assert "old" in result.result, (
        f"Expected diff text in displayed result, got: {result.result!r}\n"
        f"This is the bug: napalm_deploy is reading the wrong attribute "
        f"and the operator never sees the actual diff."
    )
    assert "new" in result.result
    assert "[edit interfaces ge-0/0/0]" in result.result


def test_napalm_deploy_handles_empty_diff(tmp_path):
    """When NAPALM honestly returns an empty diff (idempotent re-deploy),
    we should print 'no diff' - and only then."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    task = _FakeTask("dc1-spine1", fake_diff="", fake_changed=False)
    result = napalm_deploy(task, build_dir=tmp_path, commit=False)

    assert "no diff" in result.result
    assert "DRY-RUN" in result.result


def test_napalm_deploy_commit_label(tmp_path):
    """Commit mode shows the commit-confirmed label, not DRY-RUN."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    task = _FakeTask("dc1-spine1", fake_diff="[edit] - foo;")
    result = napalm_deploy(task, build_dir=tmp_path, commit=True)

    assert "COMMIT-CONFIRMED" in result.result
    assert "300s rollback timer" in result.result
    assert "[edit] - foo;" in result.result


def test_napalm_deploy_passes_revert_in_only_when_committing(tmp_path):
    """Dry-run must NOT pass revert_in (otherwise we'd start a rollback
    timer on the device for a no-commit operation)."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    # Dry-run: no revert_in
    task = _FakeTask("dc1-spine1", fake_diff="some diff")
    napalm_deploy(task, build_dir=tmp_path, commit=False)
    _, kwargs = task.run_calls[0]
    assert "revert_in" not in kwargs
    assert kwargs["dry_run"] is True

    # Commit: revert_in=300
    task2 = _FakeTask("dc1-spine1", fake_diff="some diff")
    napalm_deploy(task2, build_dir=tmp_path, commit=True)
    _, kwargs2 = task2.run_calls[0]
    assert kwargs2.get("revert_in") == 300
    assert kwargs2["dry_run"] is False


def test_napalm_deploy_passes_replace_true(tmp_path):
    """Phase 3 always uses load_replace_candidate, never load_merge."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    task = _FakeTask("dc1-spine1", fake_diff="x")
    napalm_deploy(task, build_dir=tmp_path, commit=False)
    _, kwargs = task.run_calls[0]
    assert kwargs["replace"] is True


def test_napalm_deploy_loads_config_from_disk(tmp_path):
    """The config string passed to napalm_configure must be the
    contents of build/<host>.conf - we never deploy from memory."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("MARKER_CONTENT_FROM_DISK")

    task = _FakeTask("dc1-spine1", fake_diff="x")
    napalm_deploy(task, build_dir=tmp_path, commit=False)
    _, kwargs = task.run_calls[0]
    assert "MARKER_CONTENT_FROM_DISK" in kwargs["configuration"]

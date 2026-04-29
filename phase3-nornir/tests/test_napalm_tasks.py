"""Mocked NAPALM task tests covering liveness_check, pre_commit_backup,
and the two-stage commit-confirm flow invariants.

napalm_deploy is already exercised in test_napalm_diff_contract.py
(diff attribute, revert_in, replace, config-from-disk).  This file
covers the remaining tasks and the flow-level contracts that deploy.py
relies on.
"""


from nornir.core.task import Result

from tasks.deploy import liveness_check, napalm_deploy, REVERT_IN_SECONDS
from tasks.backup import pre_commit_backup


# --- helpers ---

class _FakeMultiResult(list):
    """Mimic Nornir's MultiResult: list with a .result shortcut."""
    @property
    def result(self):
        return self[0].result if self else None


class _FakeTask:
    """Minimal Task stand-in with configurable run() responses."""

    def __init__(self, host_name, responses=None):
        self.host = type("H", (), {"name": host_name})()
        self._responses = list(responses or [])
        self.run_calls = []

    def run(self, task, **kwargs):
        self.run_calls.append((task, kwargs))
        if self._responses:
            return self._responses.pop(0)
        return _FakeMultiResult([Result(host=self.host)])


# --- REVERT_IN constant ---

def test_revert_in_is_300():
    """The commit-confirmed timer must be exactly 300s (5 min).
    deploy.py, CI smoke, and the Phase 6 plan all depend on this."""
    assert REVERT_IN_SECONDS == 300


# --- liveness_check ---

def test_liveness_check_returns_hostname_and_model():
    """liveness_check proves SSH works by fetching napalm_get facts."""
    facts_result = Result(
        host=None,
        result={"facts": {"hostname": "dc1-spine1", "model": "EX9214"}},
    )
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([facts_result])])

    result = liveness_check(task)

    assert not result.failed
    assert "dc1-spine1" in result.result
    assert "EX9214" in result.result
    assert "alive" in result.result


def test_liveness_check_calls_napalm_get_with_facts():
    facts_result = Result(
        host=None,
        result={"facts": {"hostname": "dc1-leaf1", "model": "vJunosSwitch"}},
    )
    task = _FakeTask("dc1-leaf1", responses=[_FakeMultiResult([facts_result])])
    liveness_check(task)

    assert len(task.run_calls) == 1
    _, kwargs = task.run_calls[0]
    assert kwargs["getters"] == ["facts"]


def test_liveness_check_propagates_missing_keys():
    """facts dict with missing keys should not crash -- get() returns None."""
    facts_result = Result(host=None, result={"facts": {}})
    task = _FakeTask("dc1-spine2", responses=[_FakeMultiResult([facts_result])])

    result = liveness_check(task)

    assert "hostname=None" in result.result
    assert "model=None" in result.result


# --- pre_commit_backup ---

def test_pre_commit_backup_writes_snapshot(tmp_path):
    running_config = "system { host-name dc1-leaf1; }\ninterfaces { }\n"
    config_result = Result(
        host=None,
        result={"config": {"running": running_config}},
    )
    task = _FakeTask("dc1-leaf1", responses=[_FakeMultiResult([config_result])])

    result = pre_commit_backup(task, build_dir=tmp_path)

    snap = tmp_path / "dc1-leaf1.pre-commit.conf"
    assert snap.exists()
    assert snap.read_text(encoding="utf-8") == running_config
    assert "snapshot" in result.result
    assert str(len(running_config)) in result.result


def test_pre_commit_backup_calls_napalm_get_config(tmp_path):
    config_result = Result(
        host=None,
        result={"config": {"running": "x"}},
    )
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([config_result])])
    pre_commit_backup(task, build_dir=tmp_path)

    _, kwargs = task.run_calls[0]
    assert kwargs["getters"] == ["config"]


# --- two-stage flow invariants ---

def test_dry_run_does_not_set_revert_in(tmp_path):
    """Dry-run must never start a rollback timer."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    diff_result = Result(host=None, diff="some changes")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=False)

    _, kwargs = task.run_calls[0]
    assert "revert_in" not in kwargs
    assert kwargs["dry_run"] is True


def test_commit_sets_revert_in_300(tmp_path):
    """Commit mode must start the 300s rollback timer."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    diff_result = Result(host=None, diff="some changes")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=True)

    _, kwargs = task.run_calls[0]
    assert kwargs["revert_in"] == 300
    assert kwargs["dry_run"] is False


def test_commit_always_uses_replace(tmp_path):
    """Phase 3 uses load_replace_candidate, never merge."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("x")

    diff_result = Result(host=None, diff="x")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=True)

    _, kwargs = task.run_calls[0]
    assert kwargs["replace"] is True


def test_commit_reads_config_from_build_dir(tmp_path):
    """Config bytes come from build/<host>.conf, not from memory."""
    cfg = tmp_path / "dc1-leaf2.conf"
    cfg.write_text("UNIQUE_MARKER_FOR_DISK_READ")

    diff_result = Result(host=None, diff="x")
    task = _FakeTask("dc1-leaf2", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=False)

    _, kwargs = task.run_calls[0]
    assert "UNIQUE_MARKER_FOR_DISK_READ" in kwargs["configuration"]

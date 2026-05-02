"""Mocked NAPALM task tests covering liveness_check, pre_commit_backup,
and napalm_deploy contracts under the flag-and-revert flow.

napalm_deploy diff handling lives in test_napalm_diff_contract.py;
this file covers the surrounding tasks and the commit-comment plumbing
deploy.py relies on for the marker-based rollback path.
"""


from nornir.core.task import Result

from tasks.deploy import (
    LIVENESS_REVERT_IN_SECONDS,
    LIVENESS_WAIT_SECONDS,
    liveness_check,
    napalm_deploy,
)
from tasks.backup import pre_commit_backup


# --- timing constants ---

def test_liveness_revert_in_is_120_and_multiple_of_60():
    """NAPALM Junos requires revert_in to be a multiple of 60.
    LIVENESS_REVERT_IN_SECONDS must satisfy that contract or
    `commit confirmed` will reject the value at the device."""
    assert LIVENESS_REVERT_IN_SECONDS == 120
    assert LIVENESS_REVERT_IN_SECONDS % 60 == 0


def test_liveness_wait_leaves_headroom():
    """The wait MUST be strictly less than the revert deadline. We need
    time after the wait to run liveness RPCs and issue the confirm.
    30 s wait + 120 s deadline gives ~90 s of headroom."""
    assert LIVENESS_WAIT_SECONDS == 30
    assert LIVENESS_WAIT_SECONDS < LIVENESS_REVERT_IN_SECONDS


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


# --- napalm_deploy plumbing ---

def test_dry_run_does_not_set_revert_in_or_commit_message(tmp_path):
    """Dry-run must never start a rollback timer or write a commit comment."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    diff_result = Result(host=None, diff="some changes")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=False, revert_in=120)

    _, kwargs = task.run_calls[0]
    # Even when revert_in is passed, dry-run must NOT start a timer on
    # the device -- the dry-run path doesn't issue a real commit.
    assert "revert_in" not in kwargs
    assert "commit_message" not in kwargs
    assert kwargs["dry_run"] is True


def test_commit_without_revert_in_omits_it(tmp_path):
    """Plain --commit (no inner gate) must NOT pass revert_in. That path
    is for ad-hoc operator deploys where the human handles rollback if
    needed; the inner gate is opt-in via --liveness-gate."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    diff_result = Result(host=None, diff="some changes")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=True, commit_message="cicd-42-1")

    _, kwargs = task.run_calls[0]
    assert "revert_in" not in kwargs
    assert kwargs["dry_run"] is False


def test_commit_with_revert_in_passes_it_through(tmp_path):
    """When --liveness-gate is on, deploy.py passes revert_in=120 and
    napalm_deploy must surface it to napalm_configure as the
    `commit confirmed` timer."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    diff_result = Result(host=None, diff="some changes")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(
        task,
        build_dir=tmp_path,
        commit=True,
        commit_message="cicd-42-1",
        revert_in=120,
    )

    _, kwargs = task.run_calls[0]
    assert kwargs["revert_in"] == 120
    assert kwargs["commit_message"] == "cicd-42-1"


def test_commit_with_marker_sets_commit_message(tmp_path):
    """The marker travels through napalm_configure as commit_message and
    becomes the Junos commit log that --rollback-marker later locates."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    diff_result = Result(host=None, diff="some changes")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=True, commit_message="cicd-42-1")

    _, kwargs = task.run_calls[0]
    assert kwargs["commit_message"] == "cicd-42-1"


def test_commit_without_marker_omits_commit_message(tmp_path):
    """Local --commit (no --commit-message) does a plain commit with no
    log. Useful for ad-hoc operator deploys where the rollback path
    isn't via CI."""
    cfg = tmp_path / "dc1-spine1.conf"
    cfg.write_text("system { host-name dc1-spine1; }\n")

    diff_result = Result(host=None, diff="some changes")
    task = _FakeTask("dc1-spine1", responses=[_FakeMultiResult([diff_result])])
    napalm_deploy(task, build_dir=tmp_path, commit=True)

    _, kwargs = task.run_calls[0]
    assert "commit_message" not in kwargs


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

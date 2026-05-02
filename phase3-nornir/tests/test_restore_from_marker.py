"""Unit tests for the marker-based rollback task.

restore_from_marker walks `show system commit` (PyEZ
get_commit_information RPC), finds the entry whose log matches the
marker, computes the rollback index, and commits.

Tests mock the PyEZ Device + Config classes so we can exercise the
walk + index math + error paths without the lab. The mocks are scoped
to the imports inside restore_from_marker so the production code stays
import-free until called.
"""

import sys
import types
from xml.etree import ElementTree as ET

import pytest

from nornir.core.task import Result

import tasks.deploy as deploy_mod


class _FakeHost:
    def __init__(self, name="dc1-leaf1", hostname="172.16.18.162"):
        self.name = name
        self.hostname = hostname


class _FakeTask:
    def __init__(self, host=None):
        self.host = host or _FakeHost()


class _RecordingConfig:
    """Stand-in for jnpr.junos.utils.config.Config."""
    instances = []

    def __init__(self, dev):
        self.dev = dev
        self.rolled_back_to = None
        self.commit_comment = None
        _RecordingConfig.instances.append(self)

    def rollback(self, rb_id):
        self.rolled_back_to = rb_id

    def commit(self, comment=None):
        self.commit_comment = comment


class _FakeDevice:
    """Stand-in for jnpr.junos.Device."""

    def __init__(self, history_xml, **kwargs):
        self.kwargs = kwargs
        self.opened = False
        self.closed = False
        self._history_xml = history_xml

    def open(self):
        self.opened = True
        return self

    def close(self):
        self.closed = True

    @property
    def rpc(self):
        return _FakeRPC(self._history_xml)


class _FakeRPC:
    def __init__(self, history_xml):
        self._history_xml = history_xml

    def get_commit_information(self):
        return ET.fromstring(self._history_xml)


def _install_pyez_stubs(monkeypatch, history_xml, captured_devices=None):
    """Inject fake jnpr.junos modules into sys.modules so the lazy
    import inside restore_from_marker resolves to our fakes.
    """
    if captured_devices is None:
        captured_devices = []

    jnpr = types.ModuleType("jnpr")
    jnpr_junos = types.ModuleType("jnpr.junos")
    jnpr_junos_utils = types.ModuleType("jnpr.junos.utils")
    jnpr_junos_utils_config = types.ModuleType("jnpr.junos.utils.config")

    def _device_factory(**kwargs):
        d = _FakeDevice(history_xml, **kwargs)
        captured_devices.append(d)
        return d

    jnpr_junos.Device = _device_factory
    jnpr_junos_utils_config.Config = _RecordingConfig

    monkeypatch.setitem(sys.modules, "jnpr", jnpr)
    monkeypatch.setitem(sys.modules, "jnpr.junos", jnpr_junos)
    monkeypatch.setitem(sys.modules, "jnpr.junos.utils", jnpr_junos_utils)
    monkeypatch.setitem(sys.modules, "jnpr.junos.utils.config", jnpr_junos_utils_config)
    return captured_devices


HISTORY_MARKER_AT_INDEX_0 = """\
<commit-information>
  <commit-history>
    <sequence-number>0</sequence-number>
    <date-time>2026-05-02 12:00:00 UTC</date-time>
    <user>admin</user>
    <client>netconf</client>
    <log>cicd-42-1</log>
  </commit-history>
  <commit-history>
    <sequence-number>1</sequence-number>
    <date-time>2026-05-02 11:30:00 UTC</date-time>
    <user>admin</user>
    <client>cli</client>
  </commit-history>
  <commit-history>
    <sequence-number>2</sequence-number>
    <date-time>2026-05-02 10:00:00 UTC</date-time>
    <user>admin</user>
    <client>cli</client>
  </commit-history>
</commit-information>
"""

HISTORY_MARKER_AT_INDEX_2 = """\
<commit-information>
  <commit-history>
    <sequence-number>0</sequence-number>
    <user>admin</user>
    <log>smoke failover commit</log>
  </commit-history>
  <commit-history>
    <sequence-number>1</sequence-number>
    <user>admin</user>
    <log>another smoke commit</log>
  </commit-history>
  <commit-history>
    <sequence-number>2</sequence-number>
    <user>admin</user>
    <log>cicd-42-1</log>
  </commit-history>
  <commit-history>
    <sequence-number>3</sequence-number>
    <user>admin</user>
    <log>previous good config</log>
  </commit-history>
</commit-information>
"""

HISTORY_NO_MARKER = """\
<commit-information>
  <commit-history>
    <sequence-number>0</sequence-number>
    <user>admin</user>
    <log>some other commit</log>
  </commit-history>
  <commit-history>
    <sequence-number>1</sequence-number>
    <user>admin</user>
    <log>still not it</log>
  </commit-history>
</commit-information>
"""


@pytest.fixture(autouse=True)
def _set_creds(monkeypatch):
    monkeypatch.setenv("JUNOS_SSH_USER", "admin")
    monkeypatch.setenv("JUNOS_SSH_PASSWORD", "admin@123")


def test_marker_at_index_0_rolls_back_to_1(monkeypatch):
    """If the marker is the most recent commit, rollback target is 1
    (one step back -- the state immediately before the deploy)."""
    _RecordingConfig.instances = []
    devices = _install_pyez_stubs(monkeypatch, HISTORY_MARKER_AT_INDEX_0)

    task = _FakeTask()
    res = deploy_mod.restore_from_marker(task, "cicd-42-1")

    assert isinstance(res, Result)
    assert _RecordingConfig.instances, "Config(dev) was never instantiated"
    cfg = _RecordingConfig.instances[-1]
    assert cfg.rolled_back_to == 1
    assert cfg.commit_comment == "automated rollback before cicd-42-1"
    assert "commit-index 1" in res.result
    assert "cicd-42-1" in res.result
    assert devices[0].opened
    assert devices[0].closed


def test_marker_at_index_2_rolls_back_to_3(monkeypatch):
    """Smoke produced two intermediate commits AFTER our marker; the
    walk must still find the marker by log text and compute index 3
    (the state before commit-index 2). This is the entire reason for
    the marker approach -- a plain `rollback 1` would revert smoke's
    last commit, not our deploy."""
    _RecordingConfig.instances = []
    _install_pyez_stubs(monkeypatch, HISTORY_MARKER_AT_INDEX_2)

    task = _FakeTask()
    deploy_mod.restore_from_marker(task, "cicd-42-1")

    cfg = _RecordingConfig.instances[-1]
    assert cfg.rolled_back_to == 3


def test_marker_not_found_raises(monkeypatch):
    """A missing marker is a hard error -- we do NOT silently fall back
    to `rollback 1` because that would mask the real problem (operator
    manual commit on top of CI's commit, or a workflow bug)."""
    _RecordingConfig.instances = []
    _install_pyez_stubs(monkeypatch, HISTORY_NO_MARKER)

    task = _FakeTask()
    with pytest.raises(RuntimeError) as exc:
        deploy_mod.restore_from_marker(task, "cicd-42-1")
    assert "cicd-42-1" in str(exc.value)
    assert "not found" in str(exc.value)
    # The Config must NEVER be touched when the marker isn't found.
    for cfg in _RecordingConfig.instances:
        assert cfg.rolled_back_to is None
        assert cfg.commit_comment is None


def test_missing_credentials_raises(monkeypatch):
    """JUNOS_SSH_USER / JUNOS_SSH_PASSWORD are required env. The task
    must refuse to even open the device connection without them."""
    monkeypatch.delenv("JUNOS_SSH_USER", raising=False)
    monkeypatch.delenv("JUNOS_SSH_PASSWORD", raising=False)

    task = _FakeTask()
    with pytest.raises(RuntimeError) as exc:
        deploy_mod.restore_from_marker(task, "cicd-42-1")
    assert "JUNOS_SSH_USER" in str(exc.value)


def test_device_closed_even_when_marker_missing(monkeypatch):
    """Connection cleanup must happen on the error path too."""
    devices = _install_pyez_stubs(monkeypatch, HISTORY_NO_MARKER)

    task = _FakeTask()
    with pytest.raises(RuntimeError):
        deploy_mod.restore_from_marker(task, "cicd-42-1")

    assert devices[0].opened
    assert devices[0].closed


def test_device_kwargs_use_host_hostname_and_env_creds(monkeypatch):
    """The PyEZ Device must be constructed with the inventory hostname
    (the OOB mgmt IP injected by transform.py) plus env-driven creds.
    Loopback IPs from NetBox primary_ip4 are NOT reachable from the
    runner."""
    devices = _install_pyez_stubs(monkeypatch, HISTORY_MARKER_AT_INDEX_0)

    task = _FakeTask(host=_FakeHost(name="dc1-spine1", hostname="172.16.18.160"))
    deploy_mod.restore_from_marker(task, "cicd-42-1")

    kwargs = devices[0].kwargs
    assert kwargs["host"] == "172.16.18.160"
    assert kwargs["user"] == "admin"
    assert kwargs["passwd"] == "admin@123"
    assert kwargs["port"] == 22

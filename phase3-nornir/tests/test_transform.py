"""Tests for fabric_inventory_transform.

Pure mutation given a Host stub + env. The transform_function is the
idiomatic Nornir hook for bending NetBoxInventory2's defaults to
match the lab's actual mgmt model (real OOB IPs from env, NAPALM
driver name, SSH credentials).
"""
import pytest

from tasks.transform import fabric_inventory_transform


class HostStub:
    """Minimal stand-in for nornir.core.inventory.Host.

    The transform function only sets attributes (hostname, platform,
    username, password) and reads .name. We don't need the full Host
    machinery to test that contract.
    """
    def __init__(self, name):
        self.name = name
        self.hostname = "loopback-default"
        self.platform = "Junos"
        self.username = None
        self.password = None


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MGMT_dc1_spine1", "172.16.18.160/24")
    monkeypatch.setenv("MGMT_dc1_spine2", "172.16.18.161/24")
    monkeypatch.setenv("MGMT_dc1_leaf1", "172.16.18.162/24")
    monkeypatch.setenv("MGMT_dc1_leaf2", "172.16.18.163/24")
    monkeypatch.setenv("JUNOS_SSH_USER", "admin")
    monkeypatch.setenv("JUNOS_SSH_PASSWORD", "TestLabPass1")


def test_hostname_replaced_with_oob_mgmt(env):
    """The whole reason this transform exists: NetBoxInventory2 sets
    hostname to primary_ip4 (loopback, unreachable). Override with
    the OOB mgmt IP so NAPALM can actually SSH in."""
    h = HostStub("dc1-spine1")
    fabric_inventory_transform(h)
    assert h.hostname == "172.16.18.160"


def test_mask_stripped_from_mgmt(env):
    """MGMT_* env vars store CIDR; the transform must strip the mask
    so NAPALM gets a bare IP."""
    h = HostStub("dc1-leaf1")
    fabric_inventory_transform(h)
    assert "/" not in h.hostname
    assert h.hostname == "172.16.18.162"


def test_platform_lowercased_to_napalm_driver(env):
    """NetBox stores 'Junos' (display name); NAPALM driver is 'junos'."""
    h = HostStub("dc1-spine1")
    fabric_inventory_transform(h)
    assert h.platform == "junos"


def test_credentials_injected(env):
    h = HostStub("dc1-leaf2")
    fabric_inventory_transform(h)
    assert h.username == "admin"
    assert h.password == "TestLabPass1"


def test_dash_to_underscore_in_env_lookup(env):
    """Hostnames have dashes; env vars use underscores. The transform
    must translate."""
    h = HostStub("dc1-leaf1")
    fabric_inventory_transform(h)
    # If translation broke, hostname would still be "loopback-default"
    assert h.hostname == "172.16.18.162"


def test_missing_mgmt_env_leaves_hostname_alone(monkeypatch):
    """If a host has no MGMT_<name> env var, do NOT clobber the
    NetBox-supplied hostname (better than overwriting with empty)."""
    monkeypatch.setenv("JUNOS_SSH_USER", "admin")
    monkeypatch.setenv("JUNOS_SSH_PASSWORD", "TestLabPass1")
    monkeypatch.delenv("MGMT_dc1_unknown", raising=False)
    h = HostStub("dc1-unknown")
    fabric_inventory_transform(h)
    assert h.hostname == "loopback-default"
    # Platform and creds still get set even if mgmt is missing
    assert h.platform == "junos"
    assert h.username == "admin"


def test_missing_ssh_creds_leaves_username_none(monkeypatch):
    """No JUNOS_SSH_USER set -> username stays None. The deploy guard
    in main() catches this before NAPALM is called."""
    monkeypatch.setenv("MGMT_dc1_spine1", "172.16.18.160/24")
    monkeypatch.delenv("JUNOS_SSH_USER", raising=False)
    monkeypatch.delenv("JUNOS_SSH_PASSWORD", raising=False)
    h = HostStub("dc1-spine1")
    fabric_inventory_transform(h)
    assert h.username is None
    assert h.password is None

"""Unit tests for drift/intent.py.

intent.py is the only drift module that imports pynetbox. To test
without a running NetBox we use a small FakeNb double class that
implements only the read-only methods intent.py actually calls
(.dcim.devices.filter, .dcim.interfaces.filter, .ipam.ip_addresses.filter,
.role.slug). The fake exists in this file (not a fixture file)
because it's <60 lines and pinning it here keeps the contract
between intent.py and pynetbox visible in one place.

Per the project rule "do not mock external systems": this is a
test DOUBLE, not a mock. The double is a small but real Python
class that returns hand-built objects shaped like pynetbox.Record.
If pynetbox's read API ever changes shape, the double breaks at
its method definitions and the failure is loud.

What is intentionally NOT tested:
  - Live pynetbox HTTP calls. Those land as `live` integration
    tests in Part B-full when we have something to integration-
    test against (Part B-min is the architectural-shape phase).
"""
import sys
from pathlib import Path

import pytest

# Make `from drift.intent import ...` work without an installed
# package. Mirrors phase5-suzieq/tests/conftest.py path setup.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.intent import (  # noqa: E402
    DRIFT_TAG,
    BgpSessionIntent,
    Cable,
    CableEdge,
    DeviceIntent,
    InterfaceIntent,
    collect,
)


# ---------------------------------------------------------------------------
# Test double for pynetbox.api - read-only, hand-built records
# ---------------------------------------------------------------------------

class _Obj:
    """Tiny shim that mimics pynetbox.Record's getattr-on-dict
    pattern. Used for nested objects like device, role, status."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Endpoint:
    """A pynetbox endpoint stub - exposes .filter() and .get()."""
    def __init__(self, records):
        self._records = records

    def filter(self, **kwargs):
        out = []
        for r in self._records:
            ok = True
            for k, v in kwargs.items():
                if k == "device":  # NetBox name filter, not nested
                    device_name = r.device.name if hasattr(r, "device") else None
                    if device_name != v:
                        ok = False
                        break
                elif k == "device__tag":
                    if not _has_tag(r.device, v):
                        ok = False
                        break
                elif k == "site":
                    site = getattr(r, "site", None)
                    site_slug = site.slug if site else None
                    if site_slug != v:
                        ok = False
                        break
                elif k == "tag":
                    if not _has_tag(r, v):
                        ok = False
                        break
                elif k == "interface_id":
                    iface = getattr(r, "assigned_object", None)
                    if iface is None or iface.id != v:
                        ok = False
                        break
                elif k == "name":
                    if r.name != v:
                        ok = False
                        break
                else:
                    raise NotImplementedError(
                        f"FakeNb endpoint .filter() got unexpected kwarg: {k}"
                    )
            if ok:
                out.append(r)
        return out


def _has_tag(record, slug):
    tags = getattr(record, "tags", []) or []
    return any((getattr(t, "slug", t) == slug) for t in tags)


class FakeNb:
    """Minimal pynetbox.api shape for intent.collect()."""
    def __init__(self, devices, interfaces, ip_addresses):
        self.dcim = _Obj(
            devices=_Endpoint(devices),
            interfaces=_Endpoint(interfaces),
        )
        self.ipam = _Obj(
            ip_addresses=_Endpoint(ip_addresses),
        )


# ---------------------------------------------------------------------------
# Fixtures: a tiny believable lab (2 devices, 1 cable, 1 BGP session)
# ---------------------------------------------------------------------------

def _device(name, role_slug="leaf", site_slug="dc1", status="active"):
    return _Obj(
        name=name,
        role=_Obj(slug=role_slug),
        status=_Obj(value=status),
        site=_Obj(slug=site_slug),
        tags=[_Obj(slug=DRIFT_TAG)],
    )


def _iface(id, device, name, enabled=True, cable=None, link_peer=None, ips=None):
    iface = _Obj(
        id=id,
        device=device,
        name=name,
        enabled=enabled,
        cable=cable,
        link_peers=[link_peer] if link_peer else [],
    )
    return iface


def _ip(address, iface):
    return _Obj(address=address, assigned_object=iface)


@pytest.fixture
def two_device_lab():
    """Smallest fabric: dc1-spine1 + dc1-leaf1, one P2P cable
    between them on /31. Tests build on top of this."""
    spine = _device("dc1-spine1", role_slug="spine")
    leaf  = _device("dc1-leaf1",  role_slug="leaf")

    spine_eth = _iface(id=10, device=spine, name="ge-0/0/0", enabled=True, cable=_Obj())
    leaf_eth  = _iface(id=20, device=leaf,  name="ge-0/0/0", enabled=True, cable=_Obj())
    spine_eth.link_peers = [leaf_eth]
    leaf_eth.link_peers  = [spine_eth]

    spine_ip = _ip("10.1.4.0/31", spine_eth)
    leaf_ip  = _ip("10.1.4.1/31", leaf_eth)

    nb = FakeNb(
        devices=[spine, leaf],
        interfaces=[spine_eth, leaf_eth],
        ip_addresses=[spine_ip, leaf_ip],
    )
    return nb


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

class TestCollectDevices:
    def test_collects_tagged_devices_in_namespace(self, two_device_lab):
        intent = collect(two_device_lab, "dc1")
        names = sorted(d.name for d in intent.devices)
        assert names == ["dc1-leaf1", "dc1-spine1"]

    def test_includes_status_and_role(self, two_device_lab):
        intent = collect(two_device_lab, "dc1")
        spine = next(d for d in intent.devices if d.name == "dc1-spine1")
        assert spine.status == "active"
        assert spine.role_slug == "spine"
        assert spine.site_slug == "dc1"

    def test_skips_devices_without_drift_tag(self):
        tagged   = _device("dc1-spine1", role_slug="spine")
        untagged = _device("dc1-host1",  role_slug="server")
        untagged.tags = []  # no suzieq tag
        nb = FakeNb(devices=[tagged, untagged], interfaces=[], ip_addresses=[])
        intent = collect(nb, "dc1")
        assert {d.name for d in intent.devices} == {"dc1-spine1"}

    def test_skips_devices_in_other_site(self):
        dc1_dev = _device("dc1-spine1", site_slug="dc1")
        dc2_dev = _device("dc2-spine1", site_slug="dc2")
        nb = FakeNb(devices=[dc1_dev, dc2_dev], interfaces=[], ip_addresses=[])
        intent = collect(nb, "dc1")
        assert {d.name for d in intent.devices} == {"dc1-spine1"}


# ---------------------------------------------------------------------------
# Cables
# ---------------------------------------------------------------------------

class TestCollectCables:
    def test_one_p2p_cable_yields_one_cable(self, two_device_lab):
        intent = collect(two_device_lab, "dc1")
        assert len(intent.cables) == 1
        cable = intent.cables[0]
        endpoints = sorted([cable.a.device, cable.b.device])
        assert endpoints == ["dc1-leaf1", "dc1-spine1"]

    def test_cable_to_untagged_host_is_skipped(self):
        """Host-facing cables should not appear in the intent. The
        Suzieq poller does not poll the hosts, so reporting a
        'cable not in LLDP' drift on them would be a guaranteed
        false positive."""
        leaf  = _device("dc1-leaf1", role_slug="leaf")
        host  = _device("dc1-host1", role_slug="server")
        host.tags = []  # untagged

        leaf_eth = _iface(id=20, device=leaf, name="ge-0/0/2", enabled=True, cable=_Obj())
        host_eth = _iface(id=99, device=host, name="eth1", enabled=True, cable=_Obj())
        leaf_eth.link_peers = [host_eth]
        host_eth.link_peers = [leaf_eth]

        nb = FakeNb(devices=[leaf, host], interfaces=[leaf_eth, host_eth], ip_addresses=[])
        intent = collect(nb, "dc1")
        assert intent.cables == []

    def test_multi_cable_sort_does_not_raise_on_cableedge_comparison(self):
        """REGRESSION: an earlier version of intent.py did
        `sorted(out, key=lambda c: c.normalized())`, which returned
        a tuple of CableEdge dataclasses. CableEdge is frozen=True
        but NOT order=True (intentional: we want set membership but
        not arbitrary ordering on the dataclass), so the sort
        crashed at runtime with `'<' not supported between instances
        of 'CableEdge'` the moment the fabric had >1 cable. The fix
        sorts by a stringified key. This test pins it."""
        spine1 = _device("dc1-spine1", role_slug="spine")
        spine2 = _device("dc1-spine2", role_slug="spine")
        leaf   = _device("dc1-leaf1",  role_slug="leaf")

        s1_eth = _iface(id=10, device=spine1, name="ge-0/0/0", cable=_Obj())
        s2_eth = _iface(id=11, device=spine2, name="ge-0/0/0", cable=_Obj())
        l_eth1 = _iface(id=20, device=leaf,   name="ge-0/0/0", cable=_Obj())
        l_eth2 = _iface(id=21, device=leaf,   name="ge-0/0/1", cable=_Obj())
        s1_eth.link_peers = [l_eth1]
        l_eth1.link_peers = [s1_eth]
        s2_eth.link_peers = [l_eth2]
        l_eth2.link_peers = [s2_eth]

        nb = FakeNb(
            devices=[spine1, spine2, leaf],
            interfaces=[s1_eth, s2_eth, l_eth1, l_eth2],
            ip_addresses=[],
        )
        intent = collect(nb, "dc1")
        # Two cables, sorted - if the sort raises this whole call fails
        assert len(intent.cables) == 2

    def test_cable_dedup_across_endpoints(self):
        """Iterating both sides of the cable list must not produce
        duplicate Cable entries with reversed (a, b)."""
        spine = _device("dc1-spine1", role_slug="spine")
        leaf  = _device("dc1-leaf1",  role_slug="leaf")

        spine_eth = _iface(id=10, device=spine, name="ge-0/0/0", cable=_Obj())
        leaf_eth  = _iface(id=20, device=leaf,  name="ge-0/0/0", cable=_Obj())
        spine_eth.link_peers = [leaf_eth]
        leaf_eth.link_peers  = [spine_eth]

        nb = FakeNb(
            devices=[spine, leaf],
            interfaces=[spine_eth, leaf_eth],  # both endpoints walked
            ip_addresses=[],
        )
        intent = collect(nb, "dc1")
        assert len(intent.cables) == 1


# ---------------------------------------------------------------------------
# BGP session derivation
# ---------------------------------------------------------------------------

class TestDeriveBgp:
    def test_p2p_cable_with_31s_yields_one_bgp_session(self, two_device_lab):
        intent = collect(two_device_lab, "dc1")
        assert len(intent.bgp_sessions) == 1
        s = intent.bgp_sessions[0]
        # Canonical sorted order: lexicographic on (device, ip)
        assert {s.device_a, s.device_b} == {"dc1-spine1", "dc1-leaf1"}
        assert {s.ip_a, s.ip_b} == {"10.1.4.0", "10.1.4.1"}

    def test_cable_without_ips_yields_no_bgp_session(self):
        """Cables without IPs assigned (yet) should not generate
        spurious BGP intent. Common during NetBox prep before IPs
        are populated."""
        spine = _device("dc1-spine1", role_slug="spine")
        leaf  = _device("dc1-leaf1",  role_slug="leaf")
        spine_eth = _iface(id=10, device=spine, name="ge-0/0/0", cable=_Obj())
        leaf_eth  = _iface(id=20, device=leaf,  name="ge-0/0/0", cable=_Obj())
        spine_eth.link_peers = [leaf_eth]
        leaf_eth.link_peers  = [spine_eth]
        nb = FakeNb(devices=[spine, leaf], interfaces=[spine_eth, leaf_eth], ip_addresses=[])
        intent = collect(nb, "dc1")
        assert intent.cables == [intent.cables[0]]  # cable still found
        assert intent.bgp_sessions == []           # but no BGP intent

"""Unit tests for drift/diff.py - the pure-function comparison core.

The most testable module in the harness. Imports neither pynetbox
nor pyarrow; takes intent dataclasses + state DataFrames built from
inline dict literals as inputs. ~50 ms total runtime.

Coverage strategy: one happy-path test per dimension, plus targeted
drift-detection tests for each failure mode the dimension is
designed to catch. Each drift-detection test asserts:

  1. The right number of Drift records appear
  2. The right `dimension` and `severity` are set
  3. The `subject` string identifies the affected resource
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.diff import (  # noqa: E402
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    compare,
)
from drift.intent import (  # noqa: E402
    AnycastMacIntent,
    BgpSessionIntent,
    Cable,
    CableEdge,
    DeviceIntent,
    FabricIntent,
    InterfaceIntent,
    LoopbackRouteIntent,
    PeerIrbArpIntent,
    VniIntent,
)
from drift.state import FabricState  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _device(name, status="active", role="leaf"):
    return DeviceIntent(name=name, status=status, site_slug="dc1", role_slug=role)


def _intent(devices=None, interfaces=None, cables=None, bgp_sessions=None,
            vnis=None, loopback_routes=None, anycast_macs=None,
            peer_irb_arps=None):
    return FabricIntent(
        namespace="dc1",
        devices=devices or [],
        interfaces=interfaces or [],
        cables=cables or [],
        bgp_sessions=bgp_sessions or [],
        vnis=vnis or [],
        loopback_routes=loopback_routes or [],
        anycast_macs=anycast_macs or [],
        peer_irb_arps=peer_irb_arps or [],
    )


def _state(devices=None, interfaces=None, lldp=None, bgp=None,
           evpn_vnis=None, routes=None, macs=None, arpnd=None):
    return FabricState(
        namespace="dc1",
        devices=pd.DataFrame(devices or []),
        interfaces=pd.DataFrame(interfaces or []),
        lldp=pd.DataFrame(lldp or []),
        bgp=pd.DataFrame(bgp or []),
        evpn_vnis=pd.DataFrame(evpn_vnis or []),
        routes=pd.DataFrame(routes or []),
        macs=pd.DataFrame(macs or []),
        arpnd=pd.DataFrame(arpnd or []),
    )


# ---------------------------------------------------------------------------
# Happy path: zero drift on a clean fabric
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_perfectly_aligned_fabric_yields_zero_drift(self):
        intent = _intent(
            devices=[_device("dc1-spine1", role="spine"), _device("dc1-leaf1")],
            interfaces=[
                InterfaceIntent(device="dc1-spine1", name="ge-0/0/0", enabled=True),
                InterfaceIntent(device="dc1-leaf1",  name="ge-0/0/0", enabled=True),
            ],
            cables=[Cable(
                a=CableEdge(device="dc1-spine1", interface="ge-0/0/0"),
                b=CableEdge(device="dc1-leaf1",  interface="ge-0/0/0"),
            )],
            bgp_sessions=[BgpSessionIntent(
                device_a="dc1-leaf1",  ip_a="10.1.4.1",
                device_b="dc1-spine1", ip_b="10.1.4.0",
            )],
        )
        state = _state(
            devices=[
                {"hostname": "dc1-spine1"},
                {"hostname": "dc1-leaf1"},
            ],
            interfaces=[
                {"hostname": "dc1-spine1", "ifname": "ge-0/0/0",
                 "adminState": "up", "state": "up"},
                {"hostname": "dc1-leaf1",  "ifname": "ge-0/0/0",
                 "adminState": "up", "state": "up"},
            ],
            lldp=[
                {"hostname": "dc1-spine1", "ifname": "ge-0/0/0",
                 "peerHostname": "dc1-leaf1", "peerIfname": "ge-0/0/0"},
            ],
            bgp=[
                {"hostname": "dc1-leaf1",  "vrf": "default", "peer": "10.1.4.0",
                 "state": "Established", "afi": "ipv4", "safi": "unicast"},
                {"hostname": "dc1-spine1", "vrf": "default", "peer": "10.1.4.1",
                 "state": "Established", "afi": "ipv4", "safi": "unicast"},
            ],
        )
        drifts = compare(intent, state)
        assert drifts == [], f"expected zero drift, got: {[d.subject for d in drifts]}"


# ---------------------------------------------------------------------------
# Dimension 1: device presence
# ---------------------------------------------------------------------------

class TestDevicePresence:
    def test_modeled_but_not_polled_is_error(self):
        intent = _intent(devices=[_device("dc1-spine1", role="spine"), _device("dc1-leaf1")])
        state = _state(devices=[{"hostname": "dc1-spine1"}])
        drifts = [d for d in compare(intent, state) if d.dimension == "device_presence"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert drifts[0].subject == "dc1-leaf1"
        assert "not seen by SuzieQ" in drifts[0].detail

    def test_polled_but_not_modeled_is_warning(self):
        intent = _intent(devices=[_device("dc1-spine1", role="spine")])
        state = _state(devices=[{"hostname": "dc1-spine1"}, {"hostname": "stale-old-leaf"}])
        drifts = [d for d in compare(intent, state) if d.dimension == "device_presence"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_WARNING
        assert drifts[0].subject == "stale-old-leaf"

    def test_empty_state_devices(self):
        """Brand-new poller, no devices polled yet. Every modeled
        device should appear as a drift error."""
        intent = _intent(devices=[_device("dc1-spine1", role="spine"),
                                   _device("dc1-leaf1")])
        state = _state(devices=[])
        drifts = [d for d in compare(intent, state) if d.dimension == "device_presence"]
        assert len(drifts) == 2
        assert all(d.severity == SEVERITY_ERROR for d in drifts)


# ---------------------------------------------------------------------------
# Dimension 2: interface admin state
# ---------------------------------------------------------------------------

class TestInterfaceAdmin:
    def test_admin_state_match(self):
        intent = _intent(interfaces=[
            InterfaceIntent(device="dc1-leaf1", name="ge-0/0/0", enabled=True),
        ])
        state = _state(interfaces=[
            {"hostname": "dc1-leaf1", "ifname": "ge-0/0/0",
             "adminState": "up", "state": "up"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "interface_admin"]
        assert drifts == []

    def test_netbox_enabled_but_suzieq_admin_down_is_error(self):
        intent = _intent(interfaces=[
            InterfaceIntent(device="dc1-leaf1", name="ge-0/0/0", enabled=True),
        ])
        state = _state(interfaces=[
            {"hostname": "dc1-leaf1", "ifname": "ge-0/0/0",
             "adminState": "down", "state": "down"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "interface_admin"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert drifts[0].subject == "dc1-leaf1:ge-0/0/0"
        assert "enabled=True" in drifts[0].detail
        assert "adminState='down'" in drifts[0].detail

    def test_interface_modeled_but_not_polled_is_warning(self):
        """Could be a Junos unit suffix mismatch (NetBox: 'ge-0/0/0',
        SuzieQ: 'ge-0/0/0.0'). Downgraded to warning to avoid noise."""
        intent = _intent(interfaces=[
            InterfaceIntent(device="dc1-leaf1", name="ge-0/0/0", enabled=True),
        ])
        state = _state(interfaces=[])
        drifts = [d for d in compare(intent, state) if d.dimension == "interface_admin"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_WARNING

    def test_extra_suzieq_interfaces_are_ignored(self):
        """SuzieQ sees lo0.16384, jsrv, em0 etc that NetBox does not
        model. Those must NOT generate drift - the interface_admin
        check is one-directional (intent -> state)."""
        intent = _intent(interfaces=[])
        state = _state(interfaces=[
            {"hostname": "dc1-leaf1", "ifname": "lo0.16384",
             "adminState": "up", "state": "up"},
            {"hostname": "dc1-leaf1", "ifname": "jsrv",
             "adminState": "up", "state": "up"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "interface_admin"]
        assert drifts == []


# ---------------------------------------------------------------------------
# Dimension 3: LLDP cabling
# ---------------------------------------------------------------------------

class TestLldpTopology:
    def _cable(self, da, ia, db, ib):
        return Cable(
            a=CableEdge(device=da, interface=ia),
            b=CableEdge(device=db, interface=ib),
        )

    def test_cable_matches_lldp_neighbor(self):
        intent = _intent(cables=[self._cable("dc1-spine1", "ge-0/0/0", "dc1-leaf1", "ge-0/0/0")])
        state = _state(lldp=[
            {"hostname": "dc1-spine1", "ifname": "ge-0/0/0",
             "peerHostname": "dc1-leaf1", "peerIfname": "ge-0/0/0"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "lldp_topology"]
        assert drifts == []

    def test_missing_lldp_neighbor_is_error(self):
        """The cable is in NetBox but LLDP shows nothing. Could be
        port flap, physical break, or LLDP timer not converged."""
        intent = _intent(cables=[self._cable("dc1-spine1", "ge-0/0/0", "dc1-leaf1", "ge-0/0/0")])
        state = _state(lldp=[])
        drifts = [d for d in compare(intent, state) if d.dimension == "lldp_topology"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert "<->" in drifts[0].subject

    def test_miscabled_lldp_is_error(self):
        """The headline use case. NetBox says spine1 connects to
        leaf1, but LLDP shows it actually goes to leaf2. Both the
        intent edge AND the observed edge appear, but they don't
        match - so the intent edge generates a drift."""
        intent = _intent(cables=[self._cable("dc1-spine1", "ge-0/0/0", "dc1-leaf1", "ge-0/0/0")])
        state = _state(lldp=[
            {"hostname": "dc1-spine1", "ifname": "ge-0/0/0",
             "peerHostname": "dc1-leaf2", "peerIfname": "ge-0/0/0"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "lldp_topology"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert "dc1-leaf1" in drifts[0].subject  # the intent endpoint

    def test_lldp_unit_suffix_is_stripped(self):
        """Junos LLDP can report 'ge-0/0/0.0' (logical unit). NetBox
        cables bind to the physical 'ge-0/0/0'. The strip must
        normalize before comparison or every cable would falsely
        drift."""
        intent = _intent(cables=[self._cable("dc1-spine1", "ge-0/0/0", "dc1-leaf1", "ge-0/0/0")])
        state = _state(lldp=[
            {"hostname": "dc1-spine1", "ifname": "ge-0/0/0.0",
             "peerHostname": "dc1-leaf1", "peerIfname": "ge-0/0/0.0"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "lldp_topology"]
        assert drifts == []

    def test_empty_peer_iface_falls_back_to_device_match_with_warning(self):
        """REGRESSION GUARD for the vJunos LLDP summary-view limitation:
        `show lldp neighbors | display json` does NOT include
        lldp-remote-port-id, so SuzieQ stores empty peerIfname for
        every Junos lab device. Without the Tier B fallback the lab
        would generate one drift per fabric cable on every run.
        Verified against vJunos-switch 23.2R1.14 in Phase 5 Part B
        bring-up. Tier B downgrades to a warning so the operator
        knows the check is degraded."""
        intent = _intent(cables=[self._cable("dc1-spine1", "ge-0/0/0", "dc1-leaf1", "ge-0/0/0")])
        state = _state(lldp=[
            {"hostname": "dc1-spine1", "ifname": "ge-0/0/0",
             "peerHostname": "dc1-leaf1", "peerIfname": ""},
            {"hostname": "dc1-leaf1", "ifname": "ge-0/0/0",
             "peerHostname": "dc1-spine1", "peerIfname": ""},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "lldp_topology"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_WARNING
        assert "peer interface is unknown" in drifts[0].detail

    def test_tier_b_still_catches_wrong_device_pair(self):
        """The fallback only degrades INTERFACE-level matching. A
        cable to the wrong DEVICE is still an error even with empty
        peerIfname."""
        intent = _intent(cables=[self._cable("dc1-spine1", "ge-0/0/0", "dc1-leaf1", "ge-0/0/0")])
        state = _state(lldp=[
            {"hostname": "dc1-spine1", "ifname": "ge-0/0/0",
             "peerHostname": "dc1-leaf2", "peerIfname": ""},  # wrong leaf!
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "lldp_topology"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR

    def test_tier_a_strict_match_overrides_tier_b(self):
        """When SuzieQ DOES report peerIfname (e.g. EOS, IOS, or a
        future Junos template fix), the strict match must take
        precedence and not produce a degraded warning."""
        intent = _intent(cables=[self._cable("dc1-spine1", "ge-0/0/0", "dc1-leaf1", "ge-0/0/0")])
        state = _state(lldp=[
            {"hostname": "dc1-spine1", "ifname": "ge-0/0/0",
             "peerHostname": "dc1-leaf1", "peerIfname": "ge-0/0/0"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "lldp_topology"]
        assert drifts == []  # not even a warning


# ---------------------------------------------------------------------------
# Dimension 4: BGP session presence
# ---------------------------------------------------------------------------

class TestBgpSession:
    def _session(self):
        return BgpSessionIntent(
            device_a="dc1-leaf1",  ip_a="10.1.4.1",
            device_b="dc1-spine1", ip_b="10.1.4.0",
        )

    def test_session_established_on_both_sides(self):
        intent = _intent(bgp_sessions=[self._session()])
        state = _state(bgp=[
            {"hostname": "dc1-leaf1",  "vrf": "default", "peer": "10.1.4.0",
             "state": "Established", "afi": "ipv4", "safi": "unicast"},
            {"hostname": "dc1-spine1", "vrf": "default", "peer": "10.1.4.1",
             "state": "Established", "afi": "ipv4", "safi": "unicast"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "bgp_session"]
        assert drifts == []

    def test_missing_session_is_error(self):
        intent = _intent(bgp_sessions=[self._session()])
        state = _state(bgp=[])
        drifts = [d for d in compare(intent, state) if d.dimension == "bgp_session"]
        # Both sides of the session missing -> two drifts
        assert len(drifts) == 2
        assert all(d.severity == SEVERITY_ERROR for d in drifts)
        assert all("not present" in d.detail for d in drifts)

    def test_session_present_but_not_established(self):
        intent = _intent(bgp_sessions=[self._session()])
        state = _state(bgp=[
            {"hostname": "dc1-leaf1",  "vrf": "default", "peer": "10.1.4.0",
             "state": "OpenSent", "afi": "ipv4", "safi": "unicast"},
            {"hostname": "dc1-spine1", "vrf": "default", "peer": "10.1.4.1",
             "state": "Established", "afi": "ipv4", "safi": "unicast"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "bgp_session"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert "not Established" in drifts[0].detail
        assert "OpenSent" in drifts[0].detail


# ---------------------------------------------------------------------------
# Output stability
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dimension 5: EVPN VNI presence (Part B-full)
# ---------------------------------------------------------------------------

class TestEvpnVniDiff:
    def test_modeled_vni_present_and_up_no_drift(self):
        intent = _intent(vnis=[VniIntent(device="dc1-leaf1", vni=10010, vni_type="L2")])
        state = _state(evpn_vnis=[
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2", "state": "up"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "evpn_vni"]
        assert drifts == []

    def test_modeled_vni_missing_from_state_is_error(self):
        intent = _intent(vnis=[VniIntent(device="dc1-leaf1", vni=10010, vni_type="L2")])
        state = _state(evpn_vnis=[])  # empty
        drifts = [d for d in compare(intent, state) if d.dimension == "evpn_vni"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert "10010" in drifts[0].subject

    def test_vni_present_but_not_up_is_error(self):
        intent = _intent(vnis=[VniIntent(device="dc1-leaf1", vni=10010, vni_type="L2")])
        state = _state(evpn_vnis=[
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2", "state": "down"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "evpn_vni"]
        assert len(drifts) == 1
        assert "not up" in drifts[0].detail
        assert "down" in drifts[0].detail

    def test_extra_vni_in_state_not_in_intent_is_ignored(self):
        """One-directional check: SuzieQ-only VNIs are not flagged.
        We only care that intent VNIs exist in state, not the
        reverse. (Phase 10 multi-tenant work might add a reverse
        check.)"""
        intent = _intent(vnis=[])
        state = _state(evpn_vnis=[
            {"hostname": "dc1-leaf1", "vni": 99999, "type": "L2", "state": "up"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "evpn_vni"]
        assert drifts == []


# ---------------------------------------------------------------------------
# Dimension 6: loopback routes
# ---------------------------------------------------------------------------

class TestLoopbackRouteDiff:
    def _route(self, observer, target, prefix):
        return LoopbackRouteIntent(observer_device=observer,
                                   target_device=target, prefix=prefix)

    def test_loopback_present_in_default_vrf_no_drift(self):
        intent = _intent(loopback_routes=[
            self._route("dc1-leaf1", "dc1-spine1", "10.1.0.1/32"),
        ])
        state = _state(routes=[
            {"hostname": "dc1-leaf1", "vrf": "default",
             "prefix": "10.1.0.1/32", "protocol": "bgp"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "loopback_route"]
        assert drifts == []

    def test_missing_loopback_is_error(self):
        intent = _intent(loopback_routes=[
            self._route("dc1-leaf1", "dc1-spine1", "10.1.0.1/32"),
        ])
        state = _state(routes=[])
        drifts = [d for d in compare(intent, state) if d.dimension == "loopback_route"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert "10.1.0.1/32" in drifts[0].subject

    def test_loopback_in_non_default_vrf_does_not_count(self):
        """Underlay loopbacks live in the global RIB. A /32 in
        TENANT-1 VRF is unrelated and must NOT satisfy the
        underlay reachability check."""
        intent = _intent(loopback_routes=[
            self._route("dc1-leaf1", "dc1-spine1", "10.1.0.1/32"),
        ])
        state = _state(routes=[
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1",
             "prefix": "10.1.0.1/32", "protocol": "bgp"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "loopback_route"]
        assert len(drifts) == 1  # the global VRF check still fires

    def test_each_observer_checked_independently(self):
        """A loopback present on leaf1 but not spine1 produces a
        drift for spine1's perspective only."""
        intent = _intent(loopback_routes=[
            self._route("dc1-leaf1", "dc1-spine1", "10.1.0.1/32"),
            self._route("dc1-spine1", "dc1-leaf1", "10.1.0.3/32"),
        ])
        state = _state(routes=[
            {"hostname": "dc1-leaf1", "vrf": "default",
             "prefix": "10.1.0.1/32", "protocol": "bgp"},
            # spine1 is missing the leaf1 loopback
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "loopback_route"]
        assert len(drifts) == 1
        assert "dc1-spine1" in drifts[0].subject


# ---------------------------------------------------------------------------
# Dimension 7: anycast gateway MAC
# ---------------------------------------------------------------------------

class TestAnycastMacDiff:
    def _intent_mac(self, device, vlan, mac="00:00:5e:00:01:01"):
        return AnycastMacIntent(device=device, vlan=vlan, anycast_mac=mac)

    def test_present_in_mac_table_no_drift(self):
        intent = _intent(anycast_macs=[self._intent_mac("dc1-leaf1", 10)])
        state = _state(macs=[
            {"hostname": "dc1-leaf1", "vlan": 10,
             "macaddr": "00:00:5e:00:01:01", "oif": "esi", "flags": "remote"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "anycast_mac"]
        assert drifts == []

    def test_missing_anycast_mac_is_error(self):
        intent = _intent(anycast_macs=[self._intent_mac("dc1-leaf1", 10)])
        state = _state(macs=[])
        drifts = [d for d in compare(intent, state) if d.dimension == "anycast_mac"]
        assert len(drifts) == 1
        assert "vlan10" in drifts[0].subject
        assert "00:00:5e:00:01:01" in drifts[0].subject

    def test_macaddr_match_is_case_insensitive(self):
        """Junos returns MACs in lowercase, NetBox custom field
        might be uppercase. Both should match."""
        intent = _intent(anycast_macs=[
            self._intent_mac("dc1-leaf1", 10, mac="00:00:5E:00:01:01"),
        ])
        state = _state(macs=[
            {"hostname": "dc1-leaf1", "vlan": 10,
             "macaddr": "00:00:5e:00:01:01", "oif": "esi", "flags": "remote"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "anycast_mac"]
        assert drifts == []

    def test_wrong_vlan_is_error(self):
        """The MAC must be in the table for the SPECIFIC vlan,
        not just anywhere."""
        intent = _intent(anycast_macs=[self._intent_mac("dc1-leaf1", 10)])
        state = _state(macs=[
            {"hostname": "dc1-leaf1", "vlan": 99,  # wrong vlan
             "macaddr": "00:00:5e:00:01:01", "oif": "esi", "flags": "remote"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "anycast_mac"]
        assert len(drifts) == 1


# ---------------------------------------------------------------------------
# Dimension 8: peer leaf IRB ARP
# ---------------------------------------------------------------------------

class TestPeerIrbArpDiff:
    def _arp(self, observer, target_dev, target_ip):
        return PeerIrbArpIntent(observer_device=observer,
                                target_device=target_dev,
                                target_ip=target_ip)

    def test_peer_irb_resolved_no_drift(self):
        intent = _intent(peer_irb_arps=[
            self._arp("dc1-leaf1", "dc1-leaf2", "10.10.10.4"),
        ])
        state = _state(arpnd=[
            {"hostname": "dc1-leaf1", "ipAddress": "10.10.10.4",
             "macaddr": "2c:6b:f5:41:e8:f0", "state": "reachable"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "peer_irb_arp"]
        assert drifts == []

    def test_missing_peer_arp_entry_is_error(self):
        intent = _intent(peer_irb_arps=[
            self._arp("dc1-leaf1", "dc1-leaf2", "10.10.10.4"),
        ])
        state = _state(arpnd=[])
        drifts = [d for d in compare(intent, state) if d.dimension == "peer_irb_arp"]
        assert len(drifts) == 1
        assert drifts[0].severity == SEVERITY_ERROR
        assert "10.10.10.4" in drifts[0].subject

    def test_arp_for_different_ip_does_not_satisfy(self):
        intent = _intent(peer_irb_arps=[
            self._arp("dc1-leaf1", "dc1-leaf2", "10.10.10.4"),
        ])
        state = _state(arpnd=[
            {"hostname": "dc1-leaf1", "ipAddress": "10.10.10.99",
             "macaddr": "aa:bb:cc:dd:ee:ff", "state": "reachable"},
        ])
        drifts = [d for d in compare(intent, state) if d.dimension == "peer_irb_arp"]
        assert len(drifts) == 1


class TestOutputStability:
    def test_drifts_are_sorted_by_dimension_then_subject(self):
        """Phase 6 CI golden-file tests will rely on stable
        output ordering. Catch sort regressions early."""
        intent = _intent(
            devices=[_device("z-leaf"), _device("a-leaf")],  # intentionally mis-ordered
            cables=[
                Cable(a=CableEdge("z-leaf", "ge-0/0/0"), b=CableEdge("a-leaf", "ge-0/0/0")),
            ],
        )
        state = _state(devices=[], lldp=[])
        drifts = compare(intent, state)
        dims = [d.dimension for d in drifts]
        # All device_presence drifts come before all lldp_topology drifts
        assert dims == sorted(dims)
        # Within device_presence, subjects are sorted
        device_subjects = [d.subject for d in drifts if d.dimension == "device_presence"]
        assert device_subjects == sorted(device_subjects)


# ---------------------------------------------------------------------------
# Drift record: category field + allowlist enforcement
# ---------------------------------------------------------------------------
#
# Every Drift carries a `category` field - a coarse axis ("inventory",
# "topology", "control_plane", "overlay", "arp_nd", "meta") that a
# Phase 6 CI consumer can filter/prioritize on without enumerating
# every dimension name. The __post_init__ enforces the allowlist at
# construction time so a typo ("controlPlane" vs "control_plane")
# fails loudly on the first test run.

from drift.diff import (  # noqa: E402
    CATEGORY_ARP_ND,
    CATEGORY_CONTROL_PLANE,
    CATEGORY_INVENTORY,
    CATEGORY_META,
    CATEGORY_OVERLAY,
    CATEGORY_TOPOLOGY,
    Drift,
    _VALID_CATEGORIES,
)


class TestDriftCategory:
    """Pin the Drift category contract."""

    def test_constructor_accepts_valid_category(self):
        d = Drift(
            dimension="device_presence",
            severity=SEVERITY_ERROR,
            category=CATEGORY_INVENTORY,
            subject="dc1-leaf1",
            detail="test",
        )
        assert d.category == "inventory"

    def test_constructor_rejects_unknown_category(self):
        with pytest.raises(ValueError, match="not in"):
            Drift(
                dimension="device_presence",
                severity=SEVERITY_ERROR,
                category="bogus",
                subject="dc1-leaf1",
                detail="test",
            )

    def test_typo_rejected_at_construction(self):
        # "controlplane" (no underscore) is the kind of typo that
        # would otherwise silently break a Phase 6 category filter.
        with pytest.raises(ValueError):
            Drift(
                dimension="bgp_session",
                severity=SEVERITY_ERROR,
                category="controlplane",
                subject="x",
                detail="x",
            )

    def test_to_dict_includes_category(self):
        d = Drift(
            dimension="bgp_session",
            severity=SEVERITY_ERROR,
            category=CATEGORY_CONTROL_PLANE,
            subject="dc1-leaf1->dc1-spine1",
            detail="test",
        )
        out = d.to_dict()
        assert out["category"] == "control_plane"
        # Full shape regression - pin the key set so a future
        # refactor can't silently drop or rename the field.
        assert set(out.keys()) == {
            "dimension", "severity", "category", "subject",
            "detail", "intent", "state",
        }

    def test_all_six_categories_in_allowlist(self):
        assert _VALID_CATEGORIES == {
            "inventory",
            "topology",
            "control_plane",
            "overlay",
            "arp_nd",
            "meta",
        }

    def test_constants_match_string_values(self):
        assert CATEGORY_INVENTORY == "inventory"
        assert CATEGORY_TOPOLOGY == "topology"
        assert CATEGORY_CONTROL_PLANE == "control_plane"
        assert CATEGORY_OVERLAY == "overlay"
        assert CATEGORY_ARP_ND == "arp_nd"
        assert CATEGORY_META == "meta"


class TestDimensionCategoryMapping:
    """Pin that every diff dimension emits the right category.

    The mapping is part of the Drift contract for Phase 6 consumers.
    If a future refactor accidentally changes a dimension's category
    (e.g. moves anycast_mac from 'overlay' to 'topology'), every
    downstream filter rule breaks silently. These tests make that
    breakage loud.
    """

    def _run_with_drift(self, builder):
        """Helper: run `compare()` against an intent/state pair that
        is guaranteed to produce at least one drift, return the list."""
        intent, state = builder()
        from drift.diff import compare
        drifts = compare(intent, state)
        assert drifts, "builder must produce at least one drift"
        return drifts

    def test_device_presence_is_inventory(self):
        def builder():
            intent = FabricIntent(
                namespace="dc1",
                devices=[_device("dc1-leaf1")],
            )
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "device_presence"}
        assert cats == {"inventory"}

    def test_interface_admin_is_inventory(self):
        def builder():
            intent = FabricIntent(
                namespace="dc1",
                interfaces=[InterfaceIntent(device="dc1-leaf1",
                                            name="ge-0/0/0", enabled=True)],
            )
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "interface_admin"}
        assert cats == {"inventory"}

    def test_lldp_topology_is_topology(self):
        def builder():
            cable = Cable(
                a=CableEdge(device="dc1-leaf1", interface="ge-0/0/0"),
                b=CableEdge(device="dc1-spine1", interface="ge-0/0/1"),
            )
            intent = FabricIntent(namespace="dc1", cables=[cable])
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "lldp_topology"}
        assert cats == {"topology"}

    def test_bgp_session_is_control_plane(self):
        def builder():
            intent = FabricIntent(
                namespace="dc1",
                bgp_sessions=[BgpSessionIntent(
                    device_a="dc1-leaf1", ip_a="10.1.4.0",
                    device_b="dc1-spine1", ip_b="10.1.4.1",
                )],
            )
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "bgp_session"}
        assert cats == {"control_plane"}

    def test_evpn_vni_is_overlay(self):
        def builder():
            intent = FabricIntent(
                namespace="dc1",
                vnis=[VniIntent(device="dc1-leaf1", vni=10010, vni_type="L2")],
            )
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "evpn_vni"}
        assert cats == {"overlay"}

    def test_loopback_route_is_control_plane(self):
        def builder():
            intent = FabricIntent(
                namespace="dc1",
                loopback_routes=[LoopbackRouteIntent(
                    observer_device="dc1-leaf1",
                    target_device="dc1-spine1",
                    prefix="10.1.0.1/32",
                )],
            )
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "loopback_route"}
        assert cats == {"control_plane"}

    def test_anycast_mac_is_overlay(self):
        def builder():
            intent = FabricIntent(
                namespace="dc1",
                anycast_macs=[AnycastMacIntent(
                    device="dc1-leaf1", vlan=10,
                    anycast_mac="00:1c:73:00:00:10",
                )],
            )
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "anycast_mac"}
        assert cats == {"overlay"}

    def test_peer_irb_arp_is_arp_nd(self):
        def builder():
            intent = FabricIntent(
                namespace="dc1",
                peer_irb_arps=[PeerIrbArpIntent(
                    observer_device="dc1-leaf2",
                    target_device="dc1-leaf1",
                    target_ip="10.10.10.11",
                )],
            )
            state = FabricState(namespace="dc1")
            return intent, state
        drifts = self._run_with_drift(builder)
        cats = {d.category for d in drifts if d.dimension == "peer_irb_arp"}
        assert cats == {"arp_nd"}

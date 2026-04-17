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
    BgpSessionIntent,
    Cable,
    CableEdge,
    DeviceIntent,
    FabricIntent,
    InterfaceIntent,
)
from drift.state import FabricState  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _device(name, status="active", role="leaf"):
    return DeviceIntent(name=name, status=status, site_slug="dc1", role_slug=role)


def _intent(devices=None, interfaces=None, cables=None, bgp_sessions=None):
    return FabricIntent(
        namespace="dc1",
        devices=devices or [],
        interfaces=interfaces or [],
        cables=cables or [],
        bgp_sessions=bgp_sessions or [],
    )


def _state(devices=None, interfaces=None, lldp=None, bgp=None):
    return FabricState(
        namespace="dc1",
        devices=pd.DataFrame(devices or []),
        interfaces=pd.DataFrame(interfaces or []),
        lldp=pd.DataFrame(lldp or []),
        bgp=pd.DataFrame(bgp or []),
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

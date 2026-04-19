"""Unit tests for drift/assertions/bgp.py.

Pure-function tests using inline DataFrame fixtures. No docker,
no network, no NetBox. The bgp assertion module reads only
state.bgp.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.assertions.bgp import (  # noqa: E402
    assert_bgp_all_established,
    assert_bgp_pfx_rx_positive,
)
from drift.diff import SEVERITY_ERROR  # noqa: E402
from drift.state import FabricState  # noqa: E402


def _state(bgp_rows):
    return FabricState(
        namespace="dc1",
        bgp=pd.DataFrame(bgp_rows),
    )


# ---------------------------------------------------------------------------
# assert_bgp_all_established
# ---------------------------------------------------------------------------

class TestBgpAllEstablished:
    def test_all_established_no_drift(self):
        state = _state([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
             "state": "Established", "afi": "ipv4", "safi": "unicast", "pfxRx": 1},
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.0.1",
             "state": "Established", "afi": "l2vpn", "safi": "evpn", "pfxRx": 26},
        ])
        out = assert_bgp_all_established(state)
        assert out == []

    def test_one_session_not_established_is_error(self):
        state = _state([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
             "state": "Established", "afi": "ipv4", "safi": "unicast", "pfxRx": 1},
            {"hostname": "dc1-leaf2", "vrf": "default", "peer": "10.1.4.4",
             "state": "Active", "afi": "ipv4", "safi": "unicast", "pfxRx": 0},
        ])
        out = assert_bgp_all_established(state)
        assert len(out) == 1
        assert out[0].severity == SEVERITY_ERROR
        assert out[0].dimension == "assert_bgp_established"
        assert "dc1-leaf2" in out[0].subject
        assert "10.1.4.4" in out[0].subject
        assert "Active" in out[0].detail

    def test_empty_bgp_table_no_drift(self):
        """First-cycle case: bgp table not yet populated. No
        signal, no assertion failures."""
        state = _state([])
        out = assert_bgp_all_established(state)
        assert out == []

    def test_case_insensitive_state_match(self):
        """SuzieQ might report 'Established' (capitalized) or
        'established' in the future. Match either."""
        state = _state([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
             "state": "ESTABLISHED", "afi": "ipv4", "safi": "unicast", "pfxRx": 1},
        ])
        out = assert_bgp_all_established(state)
        assert out == []


# ---------------------------------------------------------------------------
# assert_bgp_pfx_rx_positive
# ---------------------------------------------------------------------------

class TestBgpPfxRxPositive:
    def test_all_sessions_have_prefixes_no_drift(self):
        state = _state([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
             "state": "Established", "afi": "ipv4", "safi": "unicast", "pfxRx": 1},
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.0.1",
             "state": "Established", "afi": "l2vpn", "safi": "evpn", "pfxRx": 26},
        ])
        out = assert_bgp_pfx_rx_positive(state)
        assert out == []

    def test_established_with_zero_pfx_rx_is_error(self):
        """The headline use case: session is UP but zero routes
        received. Drift won't catch this because drift only
        checks state==Established."""
        state = _state([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
             "state": "Established", "afi": "ipv4", "safi": "unicast", "pfxRx": 0},
        ])
        out = assert_bgp_pfx_rx_positive(state)
        assert len(out) == 1
        assert out[0].severity == SEVERITY_ERROR
        assert out[0].dimension == "assert_bgp_pfx_rx"
        assert "pfxRx=0" in out[0].detail
        assert "ipv4/unicast" in out[0].detail

    def test_non_established_session_skipped(self):
        """A session that is not Established fails
        assert_bgp_all_established already - don't double-report it
        here. assert_bgp_pfx_rx_positive only checks sessions that
        are otherwise healthy."""
        state = _state([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
             "state": "Active", "afi": "ipv4", "safi": "unicast", "pfxRx": 0},
        ])
        out = assert_bgp_pfx_rx_positive(state)
        assert out == []

    def test_empty_bgp_table_no_drift(self):
        state = _state([])
        out = assert_bgp_pfx_rx_positive(state)
        assert out == []

    def test_missing_pfx_rx_column_treated_as_zero(self):
        """Schema drift: if SuzieQ ever stops exposing pfxRx, we
        treat missing as 0 and emit error (loud, don't silently
        pass). The row has state=Established but no pfxRx column
        at all."""
        state = _state([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
             "state": "Established", "afi": "ipv4", "safi": "unicast"},
        ])
        out = assert_bgp_pfx_rx_positive(state)
        assert len(out) == 1

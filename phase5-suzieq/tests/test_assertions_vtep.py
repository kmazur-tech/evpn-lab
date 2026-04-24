"""Unit tests for drift/assertions/vtep.py."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.assertions.vtep import assert_vtep_remote_count  # noqa: E402
from drift.diff import SEVERITY_ERROR  # noqa: E402
from drift.state import FabricState  # noqa: E402


def _state(evpn_rows):
    return FabricState(
        namespace="dc1",
        evpn_vnis=pd.DataFrame(evpn_rows),
    )


class TestVtepRemoteCount:
    # NOTE: fixtures use `remoteVtepList` (the RAW parquet column),
    # NOT `remoteVtepCnt` (the engine-computed column). The drift
    # state reader uses direct pyarrow which bypasses suzieq's
    # engine, so the assertion must compute the count itself from
    # remoteVtepList. See module docstring for the full story.

    def test_l2_vni_with_remote_vteps_no_drift(self):
        state = _state([
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
             "state": "up", "remoteVtepList": ["10.1.0.4"]},
        ])
        out = assert_vtep_remote_count(state)
        assert out == []

    def test_l2_vni_with_zero_remote_vteps_is_error(self):
        """The headline check: leaf configured for L2 VNI but sees
        no peer VTEPs - EVPN Type-3 inclusive multicast discovery
        not converged."""
        state = _state([
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
             "state": "up", "remoteVtepList": []},
        ])
        out = assert_vtep_remote_count(state)
        assert len(out) == 1
        assert out[0].severity == SEVERITY_ERROR
        assert out[0].dimension == "assert_vtep_remote_count"
        assert "vni10010" in out[0].subject
        assert "0 remote VTEPs" in out[0].detail

    def test_l2_vni_with_missing_column_is_error(self):
        """REGRESSION GUARD against the engine-vs-raw column bug.
        Live verification on the lab 2026-04-11 showed that the raw
        parquet file does NOT carry the `remoteVtepCnt` column at
        all - it is computed by SuzieQ's engine from the raw
        `remoteVtepList`. An earlier version of this assertion
        read `remoteVtepCnt` directly and produced 4 false
        positives on a fully-converged clean lab. The assertion
        now reads remoteVtepList via a helper that handles the
        missing/None/NaN cases.

        Here we simulate the missing-column case: the row has no
        remoteVtepList at all. The helper must return 0, and the
        assertion must fire (loud rather than silent pass)."""
        state = _state([
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
             "state": "up"},
        ])
        out = assert_vtep_remote_count(state)
        assert len(out) == 1

    def test_l3_vni_excluded_regardless_of_list_contents(self):
        """REGRESSION GUARD. L3 VNIs are routing-only - they do
        not enumerate remote VTEPs by design in Junos EVPN.
        Verified live on the lab (L3VNI 5000 always has no
        remoteVtepList value). Without this exclusion the
        assertion would produce a false positive on every clean
        fabric."""
        state = _state([
            {"hostname": "dc1-leaf1", "vni": 5000, "type": "L3",
             "state": "up"},
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
             "state": "up", "remoteVtepList": ["10.1.0.4"]},
        ])
        out = assert_vtep_remote_count(state)
        # L3 skipped, L2 passes -> no drift
        assert out == []

    def test_mixed_l2_vni_some_converged_some_not(self):
        """Multi-VNI scenario: one VNI converged, one not.
        Assertion fires only on the unconverged one."""
        state = _state([
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
             "state": "up", "remoteVtepList": ["10.1.0.4"]},
            {"hostname": "dc1-leaf1", "vni": 10020, "type": "L2",
             "state": "up", "remoteVtepList": []},
        ])
        out = assert_vtep_remote_count(state)
        assert len(out) == 1
        assert "vni10020" in out[0].subject

    def test_remotevteplist_as_numpy_array(self):
        """REGRESSION GUARD. pyarrow produces remoteVtepList as a
        numpy object array, not a Python list. The counting helper
        must handle both shapes because unit tests use lists while
        live parquet reads produce arrays."""
        import numpy as np
        state = _state([
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
             "state": "up",
             "remoteVtepList": np.array(["10.1.0.4"], dtype=object)},
        ])
        out = assert_vtep_remote_count(state)
        assert out == []

    def test_empty_evpn_vni_table_no_drift(self):
        state = _state([])
        out = assert_vtep_remote_count(state)
        assert out == []

    def test_category_is_overlay(self):
        """Regression guard: Phase 6 category filters depend on this."""
        state = _state([
            {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
             "state": "up", "remoteVtepList": []},
        ])
        out = assert_vtep_remote_count(state)
        assert len(out) == 1
        assert out[0].category == "overlay"
